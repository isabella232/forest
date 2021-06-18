from typing import Optional, AsyncIterator, Any, Union
from asyncio import Queue
import asyncio
import json
import os
import urllib.parse
import random
from bidict import bidict
from aiohttp import web
import aiohttp
import phonenumbers as pn
import datastore
from forest_tables import RoutingManager, PaymentsManager
# pylint: disable=line-too-long,too-many-instance-attributes, import-outside-toplevel, fixme, redefined-outer-name

HOSTNAME = open("/etc/hostname").read().strip()  #  FLY_ALLOC_ID
admin = "+16176088864"  # (sylvie; ilia is +15133278483)


def trueprint(*args: Any, **kwargs: Any) -> None:
    print(*args, **kwargs, file=open("/dev/stdout", "w"))


class Message:
    """Represents a Message received from signal-cli, optionally containing a command with arguments."""

    def __init__(self, blob: dict) -> None:
        self.envelope = envelope = blob.get("envelope", {})
        # {'envelope': {'source': '+15133278483', 'sourceDevice': 2, 'timestamp': 1621402445257, 'receiptMessage': {'when': 1621402445257, 'isDelivery': True, 'isRead': False, 'timestamps': [1621402444517]}}}
        self.source: str = envelope.get("source")
        self.ts = round(envelope.get("timestamp", 0) / 1000)
        msg = envelope.get("dataMessage", {})
        self.full_text = self.text = msg.get("message", "")
        # self.reactions: dict[str, str] = {}
        self.receipt = envelope.get("receiptMessage")
        self.group: Optional[str] = msg.get("groupInfo", {}).get("groupId")
        if self.group:
            trueprint("saw group: ", self.group)
        self.quoted_text = msg.get("quote", {}).get("text")
        if self.quoted_text:
            trueprint("saw quote: ", self.quoted_text)
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
        return f"<{self.envelope}>"


groupid_to_external_number: bidict[str, str] = bidict()


class Session:
    """
    Represents a Signal-CLI session
    Creates database connections for managing signal keys and payments.
    """

    def __init__(self, bot_number: str) -> None:
        self.bot_number = bot_number
        self.datastore = datastore.SignalDatastore(bot_number)
        self.proc: Optional[asyncio.Process] = None
        self.signalcli_output_queue: Queue[Message] = Queue()
        self.signalcli_input_queue: Queue[str] = Queue()
        self.client_session = aiohttp.ClientSession()
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
            "https://api.teleapi.net/sms/send?token=" + os.environ["TELI_KEY"],
            data=payload,
        )
        response_json = await response.json()
        return response_json

    async def send_message(
        self, recipient: str, msg: Union[str, list, dict]
    ) -> None:
        """Builds send command with specified recipient and msg, writes to signal-cli."""
        if isinstance(msg, list):
            for m in msg:
                await self.send_message(recipient, m)
        if isinstance(msg, dict):
            msg = "\n".join((f"{key}:\t{value}" for key, value in msg.items()))
        json_command = json.dumps(
            {
                "command": "send",
                "recipient": [str(recipient)],
                "message": msg,
            }
        )
        await self.signalcli_input_queue.put(json_command)

    async def signalcli_output_iter(self) -> AsyncIterator[Message]:
        """Provides an asynchronous iterator over messages on the queue."""
        while True:
            message = await self.signalcli_output_queue.get()
            yield message

    async def signalcli_input_iter(self) -> AsyncIterator[str]:
        """Provides an asynchronous iterator over pending signal-cli commands"""
        while True:
            command = await self.signalcli_input_queue.get()
            yield command

    async def register(self, message: Message) -> bool:
        new_user = message.source
        usdt_price = 15.00
        # invpico = 100000000000 # doesn't work in mixin
        invnano = 100000000
        try:
            last_val = await self.client_session.get(
                "https://big.one/api/xn/v1/asset_pairs/8e900cb1-6331-4fe7-853c-d678ba136b2f"
            )
            resp_json = await last_val.json()
            mob_rate = float(resp_json.get("data")[0].get("close"))
        except aiohttp.ClientError:
            # big.one goes down sometimes, if it does... make up a price
            mob_rate = 14
        # perturb each price slightly
        mob_rate -= random.random() / 1000
        mob_price = usdt_price / mob_rate
        nmob_price = int(mob_price * invnano)
        mob_price_exact = nmob_price / invnano
        continue_message = f"The current price for a SMS number is {mob_price_exact}MOB/month. If you would like to continue, please send exactly..."
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

    async def check_target_number(self, msg: Message) -> Optional[str]:
        trueprint(msg.arg1)
        try:
            parsed = pn.parse(msg.arg1, "US")
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

    async def handle_messages(self) -> None:
        async for message in self.signalcli_output_iter():
            # open("/dev/stdout", "w").write(f"{message}\n")
            if message.source:
                maybe_routable = self.routing_manager.get_id(
                    message.source.strip("+")
                )
            else:
                maybe_routable = None
            if maybe_routable:
                numbers: Optional[list[str]] = [
                    registered.get("id") for registered in maybe_routable
                ]
            else:
                numbers = None
            if numbers and message.command == "send":
                #dest = await self.check_target_number(message)
                #if dest:
                response = await self.send_sms(
                    source=numbers[0],
                    destination=message.arg1,  # dest,
                    message_text=message.text,
                )
                # sms_uuid = response.get("data")
                # TODO: store message.source and sms_uuid in a queue, enable https://apidocs.teleapi.net/api/sms/delivery-notifications
                #    such that delivery notifs get redirected as responses to send command
                await self.send_message(message.source, response)
            elif numbers and message.command in ("mkgroup", "query"):
                # target_number = await self.check_target_number(message)
                # if target_number:
                if (
                    "pending" in groupid_to_external_number
                    and groupid_to_external_number["pending"] == message.arg1
                ):
                    await self.send_message(
                        message.source, "looks like we've already made a group"
                    )
                    continue
                groupid_to_external_number["pending"] = message.arg1
                cmd = {
                    "command": "updateGroup",
                    "member": [message.source],
                    "name": f"SMS with {message.arg1}",
                }
                await self.signalcli_input_queue.put(json.dumps(cmd))
            elif (
                numbers
                and message.group
                and message.group in groupid_to_external_number
            ):
                await self.send_sms(
                    source=numbers[0],
                    destination=groupid_to_external_number[message.group],
                    message_text=message.text,
                )
            elif (
                numbers
                and message.quoted_text
                and "source" in message.quoted_text
            ):
                destination = (
                    message.quoted_text.split("\n")[0].lstrip("source:").strip()
                )
                trueprint("destination from quote: ", destination)
                response = await self.send_sms(
                    source=numbers[0],
                    destination=destination,
                    message_text=message.text,
                )
                trueprint("sent")
                await self.send_message(message.source, response)
            elif message.command == "help":
                await self.send_message(
                    message.source,
                    """Welcome to the Forest.contact Pre-Release!\nTo get started, try /register, or /status! If you've already registered, try to send a message via /send.""",
                )
            elif message.command == "register":
                asyncio.create_task(self.register(message))
            # elif message.command = "pay":
            #     self.scratch["payments"][message.source] = True
            elif message.command == "status":
                # paid but not registered
                if self.scratch["payments"].get(message.source) and not numbers:
                    # avaiable_numbers = [
                    #     blob["number"]
                    #     for blob in teli(user / dids / list)["data"]
                    #     if not (await self.routing_manager.connection.execute(f"select id from routing where id=$1", blob["number"])
                    # ]
                    # if avaiable_numbers:
                    #     self.routing_manager.put_destination(available_numbers[0], message.source)
                    #
                    #  send_message("what area code?")
                    #  number = search_numbers(nxx=msg.arg1)[0]
                    #  order(number) # later, y/n prompt
                    #  routing_manager.put_destination(number, msg.source)
                    await self.send_message(
                        message.source,
                        [
                            "Welcome to the beta! Thank you for your payment. Please contact support to finish setting up your account by requesting to join this group. We will reach out within 12 hours.",
                            "https://signal.group/#CjQKINbHvfKoeUx_pPjipkXVspTj5HiTiUjoNQeNgmGvCmDnEhCTYgZZ0puiT-hUG0hUUwlS",
                        ],
                    )
                # registered, one number
                elif numbers and len(numbers) == 1:
                    await self.send_message(
                        message.source,
                        f'Hi {message.source}! We found {numbers[0]} registered for your user. Try "/send {message.source} Hello from Forest Contact via {numbers[0]}!".',
                    )
                # registered, many numbers
                elif numbers:
                    await self.send_message(
                        message.source,
                        f"Hi {message.source}! We found several numbers {numbers} registered for your user. Try '/send {message.source} Hello from Forest Contact via {numbers[0]}!'.",
                    )
                # not paid, not registered
                else:
                    await self.send_message(
                        message.source,
                        'We don\'t see any Forest Contact numbers for your account! If you would like to register a new number, try "/register" and following the instructions.',
                    )
            elif message.command == "printerfact":
                async with self.client_session.get(
                    "https://colbyolson.com/printers"
                ) as resp:
                    fact = await resp.text()
                await self.send_message(message.source, fact.strip())
            elif message.command:
                await self.send_message(
                    message.source,
                    f"Sorry! Command {message.command} not recognized! Try /help. \n{message}",
                )
            elif message.text:
                await self.send_message(
                    message.source, "That didn't look like a command"
                )

    async def launch_and_connect(self) -> None:
        await self.datastore.download()
        for _ in range(5):
            if os.path.exists(self.datastore.filepath):
                break
            await asyncio.sleep(1)
        COMMAND = f"/app/signal-cli --config /app --username=+{self.bot_number} --output=json stdio".split()
        self.proc = await asyncio.create_subprocess_exec(
            *COMMAND,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        print(f"started signal-cli @ {self.bot_number} with PID {self.proc.pid}")
        assert self.proc.stdout and self.proc.stdin
        asyncio.create_task(
            listen_to_signalcli(self.proc.stdout, self.signalcli_output_queue)
        )

        async for msg in self.signalcli_input_iter():
            msg_loaded = json.loads(msg)
            open("/dev/stdout", "w").write(f"input to signal: {msg_loaded}\n")
            self.proc.stdin.write(msg.encode() + b"\n")
        await self.proc.wait()

async def start_session(app: web.Application) -> None:
    app["session"] = new_session = Session(os.environ["BOT_NUMBER"])
    asyncio.create_task(new_session.launch_and_connect())
    asyncio.create_task(new_session.handle_messages())
    profile = {
        "command": "updateProfile",
        "name": "forestbot",
        # "about": "support: https://signal.group/#CjQKINbHvfKoeUx_pPjipkXVspTj5HiTiUjoNQeNgmGvCmDnEhCTYgZZ0puiT-hUG0hUUwlS",
    }
    await new_session.signalcli_input_queue.put(json.dumps(profile))


async def listen_to_signalcli(
    stream: asyncio.StreamReader, queue: Queue[Message]
) -> None:
    while True:
        line = await stream.readline()
        trueprint(line)
        if not line:
            break
        try:
            blob = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(blob, dict):  # e.g. a timestamp
            continue
        if "error" in blob:
            trueprint(blob["error"])
            continue
        if set(blob.keys()) == {"group"}:
            group = blob.get("group")
            if group and "pending" in groupid_to_external_number:
                external_number = groupid_to_external_number["pending"]
                groupid_to_external_number[group] = external_number
                trueprint(f"associated {external_number} with {group}")
            else:
                trueprint(
                    "didn't find any pending numbers to associate with group {group}"
                )
            continue
        await queue.put(Message(blob))


async def noGet(request: web.Request) -> web.Response:
    raise web.HTTPFound(location="https://signal.org/")



async def send_message_handler(request: web.Request) -> Any:
    # account = request.match_info.get("phonenumber")
    session = request.app.get("session")
    msg_data = await request.text()
    msg_obj = {x: y[0] for x, y in urllib.parse.parse_qs(msg_data).items()}
    recipient = msg_obj.get("recipient", "+15133278483")
    if session:
        await session.send_message(recipient, msg_data)
    return web.json_response({"status": "sent"})


async def inbound_handler(request: web.Request) -> web.Response:
    msg_data = await request.text()
    # parse query-string encoded sms/mms into object
    msg_obj = {x: y[0] for x, y in urllib.parse.parse_qs(msg_data).items()}
    # if it's a raw post (debugging / oops / whatnot - not a query string)
    if not msg_obj:
        # stick the contents under the message key
        msg_obj["message"] = msg_data
    destination = msg_obj.get("destination")
    ## lookup sms recipient to signal recipient
    maybe_dest = await RoutingManager().get_destination(destination)
    recipient = maybe_dest[0].get("destination") if maybe_dest else admin
    msg_obj["maybe_dest"] = str(maybe_dest)
    session = request.app.get("session")
    if session:
        group = groupid_to_external_number.inverse.get(msg_obj["source"])
        if group:
            cmd = {
                "command": "send",
                "message": msg_obj["message"],
                "group": group,
            }
            await session.signalcli_input_queue.put(json.dumps(cmd))
        else:
            # send hashmap as signal message with newlines and tabs and stuff
            await session.send_message(recipient, msg_obj)
        return web.Response(text="TY!")
    # TODO: return non-200 if no delivery receipt / ok crypto state, let teli do our retry
    # no live worker sessions
    await request.app["client_session"].post(
        "https://counter.pythia.workers.dev/post", data=msg_data
    )
    return web.Response(status=504, text="Sorry, no live workers.")


app = web.Application()

app.on_startup.append(start_session)
app.on_startup.append(datastore.start_memfs)
app.on_startup.append(datastore.start_queue_monitor)
app.on_shutdown.append(datastore.on_shutdown)

app.add_routes(
    [
        web.get("/", noGet),
        web.post("/user/{phonenumber}", send_message_handler),
        web.post("/inbound", inbound_handler),
    ]
)

app["session"] = None


if __name__ == "__main__":
    web.run_app(app, port=8080, host="0.0.0.0")
