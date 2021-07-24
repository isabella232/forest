#!/usr/bin/python3.9
import asyncio
import asyncio.subprocess as subprocess  # https://github.com/PyCQA/pylint/issues/1469
import json
import logging
import random
import signal
import sys
import os
import urllib.parse
from asyncio import Queue
from typing import Any, AsyncIterator, Optional, Union

import aiohttp
import phonenumbers as pn
import termcolor
from aiohttp import web

import utils
import datastore
from forest_tables import GroupRoutingManager, PaymentsManager, RoutingManager
from utils import get_secret


JSON = dict[str, Any]


class Message:
    """Represents a Message received from signal-cli, optionally containing a command with arguments."""

    def __init__(self, blob: dict) -> None:
        self.blob = blob
        self.envelope = envelope = blob.get("envelope", {})
        # {'envelope': {'source': '+15133278483', 'sourceDevice': 2, 'timestamp': 1621402445257, 'receiptMessage': {'when': 1621402445257, 'isDelivery': True, 'isRead': False, 'timestamps': [1621402444517]}}}
        self.source: str = envelope.get("source")
        self.name: str = envelope.get("sourceName") or self.source
        msg = envelope.get("dataMessage", {})
        self.timestamp = envelope.get("timestamp")
        self.full_text = self.text = msg.get("message", "")
        # self.reactions: dict[str, str] = {}
        self.receipt = envelope.get("receiptMessage")
        self.group: Optional[str] = msg.get("groupInfo", {}).get("groupId")
        if self.group:
            logging.info("saw group: %s", self.group)
        self.quoted_text = msg.get("quote", {}).get("text")
        self.command: Optional[str] = None
        self.tokens: Optional[list[str]] = None
        if self.text and self.text.startswith("/"):
            command, *self.tokens = self.text.split(" ")
            self.command = command[1:]  # remove /
            self.arg1 = self.tokens[0] if self.tokens else None
            self.text = (
                " ".join(self.tokens[1:]) if len(self.tokens) > 1 else None
            )

    def __repr__(self) -> str:
        # it might be nice to prune this so the logs are easier to read
        return f"<{self.envelope}>"


class Session:
    """
k   Represents a Signal-CLI session
    Creates database connections for managing signal keys and payments.
    """

    def __init__(self, bot_number: str) -> None:
        logging.info(bot_number)

        self.bot_number = bot_number
        self.datastore = datastore.SignalDatastore(bot_number)
        self.proc: Optional[subprocess.Process] = None
        self.signalcli_output_queue: Queue[Message] = Queue()
        self.signalcli_input_queue: Queue[dict] = Queue()
        self.raw_queue: Queue[dict] = Queue()
        self.client_session = aiohttp.ClientSession()
        self.teli = utils.Teli()
        self.scratch: dict[str, dict[str, Any]] = {"payments": {}}
        self.payments_manager = PaymentsManager()
        self.routing_manager = RoutingManager()

    async def send_sms(
        self, source: str, destination: str, message_text: str
    ) -> dict[str, str]:
        """
        Send SMS via teliapi.net call and returns the response
        """
        payload = {
            "source": source,
            "destination": destination,
            "message": message_text,
        }
        response = await self.client_session.post(
            "https://api.teleapi.net/sms/send?token=" + get_secret("TELI_KEY"),
            data=payload,
        )
        response_json_all = await response.json()
        response_json = {
            k: v
            for k, v in response_json_all.items()
            if k in ("status", "segment_count")
        }  # hide how the sausage is made
        return response_json

    async def send_message(
        self,
        recipient: Optional[str],
        msg: Union[str, list, dict],
        group: Optional[str] = None,
        endsession: bool = False,
    ) -> None:
        """Builds send command with specified recipient and msg, writes to signal-cli."""
        if isinstance(msg, list):
            for m in msg:
                await self.send_message(recipient, m)
            return
        if isinstance(msg, dict):
            msg = "\n".join((f"{key}:\t{value}" for key, value in msg.items()))
        json_command: JSON = {
            "command": "send",
            "message": msg,
        }
        if endsession:
            json_command["endsession"] = True
        if group:
            json_command["group"] = group
        elif recipient:
            assert recipient == utils.signal_format(recipient)
            json_command["recipient"] = [str(recipient)]
        await self.signalcli_input_queue.put(json_command)
        return

    async def send_reaction(self, emoji: str, target_msg: Message) -> None:
        react = {
            "command": "sendReaction",
            "emoji": emoji,
            "target-author": target_msg.source,
            "target-timestamp": target_msg.timestamp,
        }
        if target_msg.group:
            react["group"] = target_msg.group
        else:
            react["recipient"] = [target_msg.source]
        await self.signalcli_input_queue.put(react)

    async def signalcli_output_iter(self) -> AsyncIterator[Message]:
        """Provides an asynchronous iterator over messages on the queue."""
        while True:
            message = await self.signalcli_output_queue.get()
            yield message

    async def signalcli_input_iter(self) -> AsyncIterator[dict]:
        """Provides an asynchronous iterator over pending signal-cli commands"""
        while True:
            command = await self.signalcli_input_queue.get()
            yield command

    async def check_target_number(self, msg: Message) -> Optional[str]:
        logging.info(msg.arg1)
        try:
            parsed = pn.parse(msg.arg1, "US")  # fixme: use PhoneNumberMatcher
            assert pn.is_valid_number(parsed)
            number = pn.format_number(parsed, pn.PhoneNumberFormat.E164)
            return number
        except (pn.phonenumberutil.NumberParseException, AssertionError):
            await self.send_message(
                msg.source,
                f"{msg.arg1} doesn't look a valid number or user. "
                "did you include the country code?",
            )
            return None

    async def do_register(self, message: Message) -> bool:
        """register for a phone number"""
        new_user = message.source
        usdt_price = 15.00
        # invpico = 100000000000 # doesn't work in mixin
        invnano = 100000000
        try:
            last_val = await self.client_session.get(
                "https://big.one/api/xn/v1/asset_pairs/8e900cb1-6331-4fe7-853c-d678ba136b2f"
            )
            resp_json = await last_val.json()
            mob_rate = float(resp_json.get("data").get("ticker").get("close"))
        except (
            aiohttp.ClientError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as e:
            logging.error(e)
            # big.one goes down sometimes, if it does... make up a price
            mob_rate = 14
        # perturb each price slightly
        mob_rate -= random.random() / 1000
        mob_price = usdt_price / mob_rate
        nmob_price = int(mob_price * invnano)
        mob_price_exact = nmob_price / invnano
        continue_message = f"The current price for a SMS number is {mob_price_exact}MOB/month. If you would like to continue, please send exactly..."
        # dunno if we want to generate new wallets? what happens if a user overpays?
        await self.send_message(
            new_user,
            [
                continue_message,
                f"{mob_price_exact}",
                "to",
                "nXz8gbcAfHQQUwTHuQnyKdALe5oXKppDn9oBRms93MCxXkiwMPnsVRp19Vrmb1GX6HdQv7ms83StXhwXDuJzN9N7h3mzFnKsL6w8nYJP4q",
                "Upon payment, you will be able to select the area code for your new phone number!",
            ],
        )
        # check for payments every 10s for 1hr
        for _ in range(360):
            payment_done = await self.payments_manager.get_payment(
                nmob_price * 1000
            )
            if payment_done:
                payment_done = payment_done[0]
                await self.send_message(
                    new_user,
                    [
                        "Thank you for your payment! Please save this transaction ID for your records and include it with any customer service requests. Without this payment ID, it will be harder to verify your purchase.",
                        f"{payment_done.get('transaction_log_id')}",
                        'Please finish setting up your account at your convenience with the "/status" command.',
                    ],
                )
                self.scratch["payments"][new_user] = payment_done.get(
                    "transaction_log_id"
                )
                return True
            await asyncio.sleep(10)
        return False

    async def do_printerfact(self, _: Message) -> str:
        async with self.client_session.get(
            "https://colbyolson.com/printers"
        ) as resp:
            fact = await resp.text()
        return fact.strip()

    async def do_status(self, message: Message) -> Union[list[str], str]:
        numbers: list[str] = [
            registered.get("id")
            for registered in await self.routing_manager.get_id(message.source)
        ]
        # paid but not registered
        if self.scratch["payments"].get(message.source) and not numbers:
            return [
                "Welcome to the beta! Thank you for your payment. Please contact support to finish setting up your account by requesting to join this group. We will reach out within 12 hours.",
                "https://signal.group/#CjQKINbHvfKoeUx_pPjipkXVspTj5HiTiUjoNQeNgmGvCmDnEhCTYgZZ0puiT-hUG0hUUwlS",
                #    "Alternatively, try /order <area code>",
            ]
        if numbers and len(numbers) == 1:
            # registered, one number
            return f'Hi {message.name}! We found {numbers[0]} registered for your user. Try "/send {message.source} Hello from Forest Contact via {numbers[0]}!".'
        # registered, many numbers
        if numbers:
            return f"Hi {message.name}! We found several numbers {numbers} registered for your user. Try '/send {message.source} Hello from Forest Contact via {numbers[0]}!'."
        # not paid, not registered
        return (
            "We don't see any Forest Contact numbers for your account!"
            " If you would like to register a new number, "
            'try "/register" and following the instructions.'
        )

    async def do_help(self, _: Message) -> str:
        return (
            "Welcome to the Forest.contact Pre-Release!\n"
            "To get started, try /register, or /status! "
            "If you've already registered, try to send a message via /send."
            ""
        )

    async def do_pay(self, message: Message) -> str:
        if message.arg1 == "shibboleth":
            self.scratch["payments"][message.source] = True
            return "...thank you for your payment"
        if message.arg1 == "sibboleth":
            return "sending attack drones to your location"
        return "no"

    async def do_order(self, msg: Message) -> str:
        """usage: /order <area code>"""
        if not (msg.arg1 and len(msg.arg1) == 3 and msg.arg1.isnumeric()):
            return """usage: /order <area code>"""
        if msg.source not in self.scratch["payments"]:
            # this needs to check if there are *unfulfilled* payments
            return "make a payment with /register first"
        await self.routing_manager.sweep_expired_destinations()
        available_numbers = [
            num
            for record in await self.routing_manager.get_available()
            if (num := record.get("id")).startswith(msg.arg1)
        ]
        if available_numbers:
            number = available_numbers[0]
            await self.send_message(msg.source, f"found {number} for you...")
        else:
            numbers = await self.teli.search_numbers(area_code=msg.arg1, limit=1)
            if not numbers:
                return "sorry, no numbers for that area code"
            number = numbers[0]
            await self.send_message(msg.source, f"found {number}")
            await self.routing_manager.intend_to_buy(number)
            buy_info = await self.teli.buy_number(number)
            await self.send_message(msg.source, f"bought {number}")
            if "error" in buy_info:
                await self.routing_manager.delete(number)
                return f"something went wrong: {buy_info}"
            await self.routing_manager.mark_bought(number)
        await self.teli.set_sms_url(number, utils.URL + "/inbound")
        await self.routing_manager.set_destination(number, msg.source)
        if await self.routing_manager.get_destination(number):
            return f"you are now the proud owner of {number}"
        return "db error?"

    if not get_secret("ORDER"):
        del do_order, do_pay

    async def do_send(self, message: Message) -> Union[str, dict]:
        numbers = [
            registered.get("id")
            for registered in await self.routing_manager.get_id(message.source)
        ]
        dest = await self.check_target_number(message)
        if dest:
            response = await self.send_sms(
                source=numbers[0],
                destination=dest,
                message_text=message.text,
            )
            await self.send_reaction("📤", message)
            # sms_uuid = response.get("data")
            # TODO: store message.source and sms_uuid in a queue, enable https://apidocs.teleapi.net/api/sms/delivery-notifications
            #    such that delivery notifs get redirected as responses to send command
            return response
        return "couldn't parse that number"

    async def handle_messages(self) -> None:
        async for message in self.signalcli_output_iter():
            if message.source:
                maybe_routable = await self.routing_manager.get_id(
                    message.source
                )
                numbers: Optional[list[str]] = [
                    registered.get("id") for registered in maybe_routable
                ]
            else:
                maybe_routable = None
                numbers = None
            if (
                numbers
                and message.command in ("mkgroup", "query")
                and utils.get_secret("GROUPS")
            ):
                target_number = await self.check_target_number(message)
                if target_number:
                    cmd = {
                        "command": "updateGroup",
                        "member": [message.source],
                        "name": f"SMS with {target_number} via {numbers[0]}",
                    }
                    await self.signalcli_input_queue.put(cmd)
                    await self.send_reaction("👥", message)
                    await self.send_message(
                        message.source, "invited you to a group"
                    )
            elif numbers and message.group:
                group = await group_routing_manager.get_sms_route_for_group(
                    message.group
                )
                if group:
                    await self.send_sms(
                        source=group[0].get("our_sms"),
                        destination=group[0].get("their_sms"),
                        message_text=message.text,
                    )
                    await self.send_reaction("📤", message)
            elif (
                numbers
                and message.quoted_text
                and "source" in message.quoted_text
            ):
                pairs = [
                    line.split(":") for line in message.quoted_text.split("\n")
                ]
                quoted = {key: value.strip() for key, value in pairs}
                logging.info("destination from quote: %s", quoted["destination"])
                response = await self.send_sms(
                    source=quoted["destination"],
                    destination=quoted["source"],
                    message_text=message.text,
                )
                logging.info("sent")
                await self.send_reaction("📤", message)
                await self.send_message(message.source, response)
            elif message.command == "register":
                # need to abstract this into a decorator or something
                # like def do_register(self, msg): asyncio.cerate_task(self.register(message))
                asyncio.create_task(self.do_register(message))
            elif message.command:
                if hasattr(self, "do_" + message.command):
                    command_response = await getattr(
                        self, "do_" + message.command
                    )(message)
                else:
                    command_response = f"Sorry! Command {message.command} not recognized! Try /help."
                await self.send_message(message.source, command_response)
            elif message.text == "TERMINATE":
                await self.send_message(message.source, "signal session reset")
            elif message.text:
                await self.send_message(
                    message.source, "That didn't look like a command"
                )

    async def launch_and_connect(self) -> None:
        logging.info("in launch_and_connect")
        loop = asyncio.get_running_loop()
        logging.info("got running loop")
        # things that don't work: loop.add_signal_handler(async_shutdown) - TypeError
        # signal.signal(sync_signal_handler) - can't interact with loop
        loop.add_signal_handler(signal.SIGINT, self.sync_signal_handler)
        logging.info("added signal handler, downloading...")

        await self.datastore.download()
        baseCmd = f"./signal-cli --config . --username={self.bot_number} "
        # requires graal fix
        name = "localbot" if utils.LOCAL else "forestbot"
        profileCmd = (
            baseCmd + "--output=plain-text updateProfile "
            f"--given-name {name} --family-name {utils.get_secret('ENV')} --avatar avatar.png"
        ).split()
        logging.info(" ".join(profileCmd))
        profileProc = await asyncio.create_subprocess_exec(*profileCmd)
        logging.info(await profileProc.communicate())
        COMMAND = (baseCmd + "--output=json stdio").split()
        logging.info(COMMAND)
        self.proc = await asyncio.create_subprocess_exec(
            *COMMAND,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        logging.info(
            "started signal-cli @ %s with PID %s",
            self.bot_number,
            self.proc.pid,
        )
        assert self.proc.stdout and self.proc.stdin
        asyncio.create_task(
            listen_to_signalcli(
                self.proc.stdout,
                self.signalcli_output_queue,
                self.raw_queue,
            )
        )
        async for msg in self.signalcli_input_iter():
            logging.info("input to signal: %s", msg)
            self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await self.proc.wait()

    async def async_shutdown(self, *unused: Any, wait: bool = False) -> None:
        logging.info("starting async_shutdown")
        await self.datastore.upload()
        if self.proc:
            try:
                self.proc.kill()
                if wait:
                    await self.proc.wait()
                    await self.datastore.upload()
            except ProcessLookupError:
                logging.info("no process")
        await self.datastore.mark_freed()
        logging.info("=============exited===================")
        sys.exit(0)
        logging.info(
            "called sys.exit but still running, os.kill sigint to %s",
            os.getpid(),
        )
        os.kill(os.getpid(), signal.SIGINT)
        logging.info("still running after os.kill, trying os._exit")
        os._exit(1)

    sigints = 0

    def sync_signal_handler(self, *unused: Any) -> None:
        logging.info("handling sigint. sigints: %s", self.sigints)
        self.sigints += 1
        try:
            loop = asyncio.get_running_loop()
            logging.info("got running loop, scheduling async_shutdown")
            asyncio.run_coroutine_threadsafe(self.async_shutdown(), loop)
        except RuntimeError:
            asyncio.run(self.async_shutdown())
        if self.sigints >= 3:
            sys.exit(1)
            raise KeyboardInterrupt
            logging.info(  # pylint: disable=unreachable
                "this should never get called"
            )


async def start_session(our_app: web.Application) -> None:
    try:
        number = utils.signal_format(sys.argv[1])
    except IndexError:
        number = get_secret("BOT_NUMBER")
    logging.info(number)
    our_app["session"] = new_session = Session(number)
    if utils.get_secret("MIGRATE"):
        logging.info("migrating db...")
        await new_session.routing_manager.migrate()
        rows = await new_session.routing_manager.execute(
            "SELECT id, destination FROM routing"
        )
        for row in rows if rows else []:
            new_dest = utils.signal_format(row.get("destination"))
            await new_session.routing_manager.set_destination(
                row.get("id"), new_dest
            )
        await new_session.datastore.account_interface.migrate()
        await group_routing_manager.create_table()
    asyncio.create_task(new_session.launch_and_connect())
    asyncio.create_task(new_session.handle_messages())


async def listen_to_signalcli(
    stream: asyncio.StreamReader,
    queue: Queue[Message],
    raw_queue: Optional[Queue[JSON]],
) -> None:
    while True:
        line = await stream.readline()
        # if utils.get_secret("I_AM_NOT_A_FEDERAL_AGENT"):
        logging.info("signal: %s", line.decode())
        # TODO: don't print receiptMessage
        # color non-json. pretty-print errors
        # sensibly color web traffic, too?
        # fly / db / asyncio and other lib warnings / java / signal logic and networking
        if not line:
            break
        try:
            blob = json.loads(line)
            if raw_queue:
                await raw_queue.put(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(blob, dict):  # e.g. a timestamp
            continue
        if "error" in blob:
            logging.error(termcolor.colored(blob["error"], "red"))
            continue
        if "group" in blob:
            # maybe this info should just be in Message and handled in Session
            # SMS with {number} via {number}
            their, our = blob["name"].removeprefix("SMS with ").split(" via ")
            # TODO: this needs to use number[0]
            await GroupRoutingManager().set_sms_route_for_group(
                utils.teli_format(their), utils.teli_format(our), blob["group"]
            )
            logging.info("made a new group route from %s", blob)
            continue
        await queue.put(Message(blob))


async def noGet(request: web.Request) -> web.Response:
    raise web.HTTPFound(location="https://signal.org/")


async def inbound_sms_handler(request: web.Request) -> web.Response:
    msg_data = await request.text()  # await request.post()
    # parse query-string encoded sms/mms into object
    msg_obj = {
        x: y[0]
        for x, y in urllib.parse.parse_qs(msg_data).items()
        if x in ("source", "destination", "message")
    }
    # if it's a raw post (debugging / oops / whatnot - not a query string)
    logging.info(msg_obj)
    destination = msg_obj.get("destination")
    # lookup sms recipient to signal recipient
    maybe_dest = await RoutingManager().get_destination(destination)
    if maybe_dest:
        recipient = maybe_dest[0].get("destination")
    else:
        logging.info("falling back to admin")
        recipient = get_secret("ADMIN")
        msg_obj["message"] = "destination not found for " + str(msg_obj)
    # msg_obj["maybe_dest"] = str(maybe_dest)
    session = request.app.get("session")
    if session:
        maybe_group = await group_routing_manager.get_group_id_for_sms_route(
            msg_obj["source"], msg_obj["destination"]
        )
        logging.info(maybe_group)
        if maybe_group:
            # if we can't notice group membership changes,
            # we could check if the person is still in the group
            logging.info("sending a group")
            group = maybe_group[0].get("group_id")
            await session.send_message(None, msg_obj["message"], group=group)
        else:
            # send hashmap as signal message with newlines and tabs and stuff
            await session.send_message(recipient, msg_obj)

        return web.Response(text="TY!")
    # TODO: return non-200 if no delivery receipt / ok crypto state, let teli do our retry
    # no live worker sessions
    # ...to do that, you would have to await a response from signal, which requires hooks or such
    await request.app["client_session"].post(
        "https://counter.pythia.workers.dev/post", data=msg_data
    )
    return web.Response(status=504, text="Sorry, no live workers.")


app = web.Application()

app.on_startup.append(start_session)
app.on_startup.append(datastore.start_memfs)
app.on_startup.append(datastore.start_queue_monitor)
app.add_routes(
    [
        web.get("/", noGet),
        web.post("/inbound", inbound_sms_handler),
    ]
)

app["session"] = None


if __name__ == "__main__":
    logging.info("=========================new run=======================")
    group_routing_manager = GroupRoutingManager()
    web.run_app(app, port=8080, host="0.0.0.0")
