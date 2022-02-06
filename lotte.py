from cmd import PROMPT
import logging
from urllib import request
from forest import utils
from forest.core import Message, PayBot, Response, hide, QuestionBot, requires_admin, run_bot
import openai
import os
from aiohttp import web

openai.api_key = os.getenv("OPENAI_API_KEY", "")

class Lotte(PayBot):

    bios = {"lotte":("The following is a conversation with an AI assistant named Lotte. "
                "lotte is a young girl who is helpful, creative, clever, funny, very friendly, a writer and anarcho-communist. lotte's older sister is called Imogen and she's an artist. Lotte's best friend is Rowena the faerie.\n\n"
                "SOURCE: Hello, who are you?\nlotte: My name is lotte, I'm an AI that loves having rivetting intellectual discussions. How can I help you today?"
                # "SOURCE: MESSAGE \nLotte: "
            ),"rowena":("The following is a conversation with a faerie named Rowena. "
                "rowena is a faerie with mushroom like appearance, gossamer wings and a pointy tail. She likes to play tricks on people, and whilst she might come across as mean she really wants everyone to have fun. Rowena's best friend is Lotte.\n\n"
                "SOURCE: Hello, who are you?\nrowena: My name is rowena, I'm a faerie sort of creature, what sort of creature are you?"
                # "SOURCE: MESSAGE \nRowena: "
            ),
            }

    current_fronter="lotte"

    conversation=[]
    
    
    async def chat(self, msg: Message, fronter) -> Response:

        prompt = self.bios[fronter].replace("SOURCE",msg.source)
        for line in self.conversation:
            prompt= prompt + "\n" + line
        prompt = prompt + "\n" + f"{msg.source}: {msg.text} \n{fronter}: "
        self.conversation.append(f"{msg.source}: {msg.text}")

        logging.info(prompt)
        
        

        response = openai.Completion.create(  # type: ignore
            engine="curie",
            prompt = prompt,
            temperature=0.9,
            max_tokens=240,
            top_p=1,
            frequency_penalty=0.01,
            presence_penalty=0.6,
            stop=["\n", f"{msg.source}:", fronter],
        )
        answer= response["choices"][0]["text"].strip().replace("AI:", "\nAI:").replace(f"{msg.source}:", f"\n{msg.source}:")
        logging.info(answer)
        self.conversation.append(f"{fronter}: {answer}")
        for line in self.conversation:
            logging.info(line)

        return answer # + f"\n and here's the extra:\n {str(len(self.conversation))} \n {self.conversation[len(self.conversation)-1]}"

    async def do_rowena(self,msg:Message) -> str:
        """Chat With Rowena the Faerie"""
        return await self.chat(msg,"rowena")

    async def do_lotte(self,msg:Message) -> str:
        """Chat With Lotte the AI"""
        return await self.chat(msg,"lotte")

    async def do_switch(self,msg:Message) -> Response:
        requested_fronter=msg.arg1.lower()
        if requested_fronter in self.bios:
            if requested_fronter == self.current_fronter:
                return "they're already in front"
            self.current_fronter=requested_fronter
            return f"{self.current_fronter} is now in front"

        else:
            return "there's no one by that name in our system"

    async def do_clear(self,msg:Message) -> Response:
        self.conversation=[]
        return "conversation history has been cleared"

    

    async def default(self, msg: Message) -> Response:

        

        if msg.arg0 or msg.full_text:
            msg.text = msg.full_text
            return await self.chat(msg,self.current_fronter)
        return await super().default(msg)


if __name__ == "__main__":
    run_bot(Lotte)