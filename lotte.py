import time
import os
import logging
from typing import Dict
from forest.core import Message, PayBot, Response, run_bot
import openai


openai.api_key = os.getenv("OPENAI_API_KEY", "")


class Lotte(PayBot):
    """A narrative oriented GPT chatbot with semipersistent memory"""

    bios = {
        "Lotte": (
            "The following is a conversation with an AI assistant named Lotte. "
            "Lotte is a young girl who is helpful, creative, clever, funny, very friendly, a writer and anarcho-communist. Lotte's older sister is called Imogen and she's an artist. Lotte's best friend is Rowena the faerie.\n\n"
            "SOURCE: Hello, who are you?\nLotte: My name is Lotte, I'm an AI that loves having rivetting intellectual discussions. How can I help you today?"
            # "SOURCE: MESSAGE \nLotte: "
        ),
        "Rowena": (
            "The following is a conversation with a faerie named Rowena. "
            "Rowena is a faerie with mushroom like appearance, gossamer wings and a pointy tail."
            "She likes to play tricks on people, and whilst she might come across as mean she really wants everyone to have fun. Rowena's best friend is Lotte.\n\n"
            "SOURCE: Hello, who are you?\nRowena: My name is Rowena, I'm a faerie sort of creature, what sort of creature are you?"
            # "SOURCE: MESSAGE \nRowena: "
        ),
    }

    current_fronter = "Lotte"

    conversation: Dict[float, str] = {}

    # 0 means it's free, 1 means it's occupied
    namepool = {
        "Taylor": 0,
        "Morgan": 0,
        "Lee": 0,
        "Rhianon": 0,
        "Georgie": 0,
        "Moritz": 0,
    }

    async def chat(self, msg: Message, fronter: str) -> Response:
        """Organises the conversation and sends responses"""

        prompt = self.bios[fronter].replace("SOURCE", msg.source)
        for _, line in self.conversation.items():
            prompt = prompt + "\n" + line
        prompt = prompt + "\n" + f"{msg.source}: {msg.text} \n{fronter}: "
        self.conversation[time.time()] = f"{msg.source}: {msg.text}"
        result = openai.Completion.create(  # type: ignore
            engine="curie",
            prompt=prompt,
            temperature=0.75,
            max_tokens=250,
            top_p=1,
            frequency_penalty=0.01,
            presence_penalty=0.7,
            stop=["\n", f"{msg.source}:", fronter],
        )
        answer = (
            result["choices"][0]["text"]
            .strip()
            .replace("AI:", "\nAI:")
            .replace(f"{msg.source}:", f"\n{msg.source}:")
        )

        # fronter = (
        #     fronter[0].upper() + fronter[1:]
        # )  # names being properly uppercase might help the prompt
        self.conversation[time.time()] = f"{fronter}: {answer}"
        logging.info("conversation:")
        for _, line in self.conversation.items():
            logging.info(line)

        return answer  # + f"\n and here's the extra:\n {str(len(self.conversation))} \n {self.conversation[len(self.conversation)-1]}"

    async def do_rowena(self, msg: Message) -> Response:
        """Chat With Rowena the Faerie"""
        return await self.chat(msg, "rowena")

    async def do_lotte(self, msg: Message) -> Response:
        """Chat With Lotte the AI"""
        return await self.chat(msg, "lotte")

    async def do_switch(self, msg: Message) -> Response:
        """Switches to a different character to talk to"""

        if not isinstance(msg.arg1, str):
            return "You have to specify a name!"

        requested_fronter = msg.arg1.lower()

        if requested_fronter in {k.lower() for k in self.bios}:
            if requested_fronter == self.current_fronter:
                return "they're already in front"
            self.current_fronter = requested_fronter
            return f"{self.current_fronter} is now in front"

        return "there's no one by that name here"

    async def do_clear(self) -> Response:
        """Clear the conversation history"""
        self.conversation = {}
        return "conversation history has been cleared"

    # async def summarize_history(self, conversation) -> list(str):
    #     openai.

    #     return conversation

    async def default(self, message: Message) -> Response:

        if message.arg0:
            return await self.chat(message, self.current_fronter)
        return await super().default(message)


if __name__ == "__main__":
    run_bot(Lotte)
