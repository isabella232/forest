from cmd import PROMPT
from forest import utils
from forest.core import Message, Response, hide, QuestionBot, requires_admin, run_bot
import openai
import os
from aiohttp import web

openai.api_key = os.getenv("OPENAI_API_KEY", "")

class Lotte(QuestionBot):

    bios = {}
    prompt = (
                "The following is a conversation with an AI assistant named Lotte. "
                "Lotte is a young girl who is helpful, creative, clever, funny, very friendly, a writer and anarcho-communist. Lotte's older sister is called Imogen and she's an artist.\n\n"
                # f"{msg.source}: Hello, who are you?\nAI: My name is Lotte, I'm an AI that loves having rivetting intellectual discussions. How can I help you today?\n"
                # f"{msg.source}: {msg.text}\nAI: "
            )
    bios['Lotte']=prompt
    
    async def do_c(self, msg: Message) -> str:

        prompt = (
            "The following is a conversation with an AI assistant named Lotte. "
            "Lotte is a young girl who is helpful, creative, clever, funny, very friendly, a writer and anarcho-communist. Lotte's older sister is called Imogen and she's an artist.\n\n"
            f"{msg.source}: Hello, who are you?\nAI: My name is Lotte, I'm an AI that loves having rivetting intellectual discussions. How can I help you today?\n"
            f"{msg.source}: {msg.text}\nAI: ")
        

        response = openai.Completion.create(  # type: ignore
            engine="davinci",
            prompt = prompt,
            temperature=0.9,
            max_tokens=240,
            top_p=1,
            frequency_penalty=0.01,
            presence_penalty=0.6,
            stop=["\n", f"{msg.source}:", " AI:"],
        )
        answer= response["choices"][0]["text"].strip().replace("AI:", "\nAI:").replace(f"{msg.source}:", f"\n{msg.source}:")
        return answer

    async def default(self, msg: Message) -> Response:

        if msg.arg0 or msg.full_text:
            msg.text = msg.full_text
            return await self.do_c(msg)
        return await super().default(msg)


if __name__ == "__main__":
    run_bot(Lotte)