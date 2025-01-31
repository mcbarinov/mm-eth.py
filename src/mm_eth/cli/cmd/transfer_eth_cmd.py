import sys
import time
from pathlib import Path
from typing import Annotated, Self

import mm_crypto_utils
from loguru import logger
from mm_crypto_utils import AddressToPrivate, TxRoute
from mm_std import BaseConfig, Err, Ok, utc_now
from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from mm_eth import rpc
from mm_eth.account import address_from_private, is_address
from mm_eth.cli import calcs, cli_utils, print_helpers, rpc_helpers
from mm_eth.cli.validators import Validators
from mm_eth.tx import sign_tx
from mm_eth.utils import from_wei_str


class Config(BaseConfig):
    nodes: Annotated[list[str], BeforeValidator(Validators.nodes())]
    chain_id: int
    routes: Annotated[list[TxRoute], BeforeValidator(Validators.routes(is_address, to_lower=True))]
    routes_from_file: Path | None = None
    routes_to_file: Path | None = None
    private_keys: Annotated[
        AddressToPrivate, Field(default_factory=AddressToPrivate), BeforeValidator(Validators.private_keys(address_from_private))
    ]
    private_keys_file: Path | None = None
    max_fee: Annotated[str, AfterValidator(Validators.valid_calc_value("base_fee"))]
    priority_fee: Annotated[str, AfterValidator(Validators.valid_calc_value())]
    max_fee_limit: Annotated[str | None, AfterValidator(Validators.valid_calc_value())] = None
    value: Annotated[str, AfterValidator(Validators.valid_calc_value("balance"))]
    value_min_limit: Annotated[str | None, AfterValidator(Validators.valid_calc_value())] = None
    gas: Annotated[str, AfterValidator(Validators.valid_calc_value("estimate"))]
    delay: Annotated[str | None, AfterValidator(Validators.valid_calc_decimal_value())] = None  # in seconds
    round_ndigits: int = 5
    log_debug: Annotated[Path | None, BeforeValidator(Validators.log_file())] = None
    log_info: Annotated[Path | None, BeforeValidator(Validators.log_file())] = None

    @property
    def from_addresses(self) -> list[str]:
        return [r.from_address for r in self.routes]

    # noinspection DuplicatedCode
    @model_validator(mode="after")
    def final_validator(self) -> Self:
        # routes
        if self.routes_from_file and self.routes_to_file:
            self.routes += TxRoute.from_files(self.routes_from_file, self.routes_to_file, is_address)
        if not self.routes:
            raise ValueError("routes is empty")

        # private keys
        if self.private_keys_file:
            self.private_keys.update(AddressToPrivate.from_file(self.private_keys_file, address_from_private))
        if not self.private_keys.contains_all_addresses(self.from_addresses):
            raise ValueError("private keys are not set for all addresses")

        return self


def run(
    config_path: str,
    *,
    print_balances: bool,
    print_config: bool,
    debug: bool,
    no_receipt: bool,
    emulate: bool,
) -> None:
    config = Config.read_config_or_exit(config_path)
    if print_config:
        config.print_and_exit({"private_keys"})

    mm_crypto_utils.init_logger(debug, config.log_debug, config.log_info)
    rpc_helpers.check_nodes_for_chain_id(config.nodes, config.chain_id)

    if print_balances:
        print_helpers.print_balances(config.nodes, config.from_addresses, round_ndigits=config.round_ndigits)
        sys.exit(0)

    return _run_transfers(config, no_receipt=no_receipt, emulate=emulate)


# noinspection DuplicatedCode
def _run_transfers(config: Config, *, no_receipt: bool, emulate: bool) -> None:
    logger.info(f"started at {utc_now()} UTC")
    logger.debug(f"config={config.model_dump(exclude={'private_keys'}) | {'version': cli_utils.get_version()}}")
    for i, route in enumerate(config.routes):
        _transfer(route=route, config=config, no_receipt=no_receipt, emulate=emulate)
        if not emulate and config.delay is not None and i < len(config.routes) - 1:
            delay_value = mm_crypto_utils.calc_decimal_value(config.delay)
            logger.debug(f"delay {delay_value} seconds")
            time.sleep(float(delay_value))
    logger.info(f"finished at {utc_now()} UTC")


# noinspection DuplicatedCode
def _transfer(*, route: TxRoute, config: Config, no_receipt: bool, emulate: bool) -> None:
    log_prefix = f"{route.from_address}->{route.to_address}"
    # get nonce
    nonce = rpc_helpers.get_nonce(config.nodes, route.from_address, log_prefix)
    if nonce is None:
        return

    # get max_fee
    max_fee = rpc_helpers.calc_max_fee(config.nodes, config.max_fee, log_prefix)
    if max_fee is None:
        return

    # check max_fee_limit
    if rpc_helpers.is_max_fee_limit_exceeded(max_fee, config.max_fee_limit, log_prefix):
        return

    # get gas
    gas = rpc_helpers.calc_gas(
        nodes=config.nodes,
        gas=config.gas,
        from_address=route.from_address,
        to_address=route.to_address,
        value=123,
        log_prefix=log_prefix,
    )
    if gas is None:
        return

    # get value
    value = rpc_helpers.calc_eth_value(
        nodes=config.nodes,
        value_str=config.value,
        address=route.from_address,
        gas=gas,
        max_fee=max_fee,
        log_prefix=log_prefix,
    )
    if value is None:
        return

    # value_min_limit
    if calcs.is_value_less_min_limit(config.value_min_limit, value, "eth", log_prefix=log_prefix):
        return

    priority_fee = calcs.calc_var_value(config.priority_fee)

    # emulate?
    if emulate:
        msg = f"{log_prefix}: emulate, value={from_wei_str(value, 'eth', config.round_ndigits)},"
        msg += f" max_fee={from_wei_str(max_fee, 'gwei', config.round_ndigits)},"
        msg += f" priority_fee={from_wei_str(priority_fee, 'gwei', config.round_ndigits)},"
        msg += f" gas={gas}"
        logger.info(msg)
        return

    debug_tx_params = {
        "nonce": nonce,
        "max_fee": max_fee,
        "priority_fee": priority_fee,
        "gas": gas,
        "value": value,
        "to": route.to_address,
        "chain_id": config.chain_id,
    }
    logger.debug(f"{log_prefix}: tx_params={debug_tx_params}")
    signed_tx = sign_tx(
        nonce=nonce,
        max_fee_per_gas=max_fee,
        max_priority_fee_per_gas=priority_fee,
        gas=gas,
        private_key=config.private_keys[route.from_address],
        chain_id=config.chain_id,
        value=value,
        to=route.to_address,
    )
    res = rpc.eth_send_raw_transaction(config.nodes, signed_tx.raw_tx, attempts=5)
    if isinstance(res, Err):
        logger.info(f"{log_prefix}: send_error: {res.err}")
        return
    tx_hash = res.ok

    if no_receipt:
        msg = f"{log_prefix}: tx_hash={tx_hash}, value={from_wei_str(value, 'ether', round_ndigits=config.round_ndigits)}"
        logger.info(msg)
    else:
        logger.debug(f"{log_prefix}: tx_hash={tx_hash}, wait receipt")
        while True:  # TODO: infinite loop if receipt_res is err
            receipt_res = rpc.get_tx_status(config.nodes, tx_hash)
            if isinstance(receipt_res, Ok):
                status = "OK" if receipt_res.ok == 1 else "FAIL"
                msg = f"{log_prefix}: tx_hash={tx_hash}, value={from_wei_str(value, 'ether', round_ndigits=config.round_ndigits)}, status={status}"  # noqa: E501
                logger.info(msg)
                break
            time.sleep(1)
