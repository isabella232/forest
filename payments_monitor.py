from typing import Optional
import json
import logging
import time
import aiohttp
import mobilecoin
import forest_tables
import utils
import mc_util

mobilecoind: mobilecoin.Client = mobilecoin.Client("http://localhost:9090/wallet", ssl=False)  # type: ignore


def get_accounts() -> None:
    assert hasattr(mobilecoind, "get_all_accounts")
    raise NotImplementedError
    # account_id = list(mobilecoind.get_all_accounts().keys())[0]  # pylint: disable=no-member # type: ignore


async def mob(data: dict) -> dict:
    better_data = {"jsonrpc": "2.0", "id": 1, **data}
    async with aiohttp.ClientSession() as session:
        req = session.post(
            "http://full-service.fly.dev/wallet",
            data=json.dumps(better_data),
            headers={"Content-Type": "application/json"},
        )
        async with req as resp:
            return await resp.json()


async def import_account() -> dict:
    params = {
        "mnemonic": utils.get_secret("MNEMONIC"),
        "key_derivation_version": "2",
        "name": "falloopa",
        "next_subaddress_index": "2",
        "first_block_index": "3500",
    }
    return await mob({"method": "import_account", "params": params})


# cache?
async def get_address() -> str:
    res = await mob({"method": "get_all_accounts"})
    acc_id = res["result"]["account_ids"][0]
    return res["result"]["account_map"][acc_id]["main_address"]


async def get_receipt_amount(receipt_str: str) -> Optional[float]:
    full_service_receipt = mc_util.b64_receipt_to_full_service_receipt(receipt_str)
    logging.debug(full_service_receipt)
    params = {
        "address": await get_address(),
        "receiver_receipt": full_service_receipt,
    }
    tx = await mob({"method": "check_receiver_receipt_status", "params": params})
    logging.debug(tx)
    if "error" in tx:
        return None
    pmob = int(tx["result"]["txo"]["value_pmob"])
    return mc_util.pmob2mob(pmob)


def get_transactions() -> dict[str, dict[str, str]]:
    raise NotImplementedError
    # mobilecoin api changed, this needs to make full-service reqs
    # return mobilecoind.get_all_transaction_logs_for_account(account_id)  # type: ignore # pylint: disable=no-member


def local_main() -> None:
    last_transactions: dict[str, dict[str, str]] = {}
    payments_manager_connection = forest_tables.PaymentsManager()
    payments_manager_connection.sync_create_table()

    while True:
        latest_transactions = get_transactions()
        for transaction in latest_transactions:
            if transaction not in last_transactions:
                unobserved_tx = latest_transactions.get(transaction, {})
                short_tx = {}
                for k, v in unobserved_tx.items():
                    if isinstance(v, list) and len(v) == 1:
                        v = v[0]
                    if isinstance(v, str) and k != "value_pmob":
                        v = v[:16]
                    short_tx[k] = v
                print(short_tx)
                # invoice = await invoice_manager.get_invoice_by_amount(value_pmob)
                # if invoice:
                #    credit = await pmob_to_usd(value_pmob)
                #    await transaction_manager.put_transaction(invoice.user, credit)
                # otherwise check if it's related to signal pay
                # otherwise, complain about this unsolicited payment to an admin or something
                payments_manager_connection.sync_put_payment(
                    short_tx["transaction_log_id"],
                    short_tx["account_id"],
                    int(short_tx["value_pmob"]),
                    int(short_tx["finalized_block_index"]),
                )
        last_transactions = latest_transactions.copy()
        time.sleep(10)


if __name__ == "__main__":
    local_main()
