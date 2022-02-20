import time
import os
import logging
from urllib import response
from forest.core import Message, PayBot, Response, run_bot
import openai



openai.api_key = os.getenv("OPENAI_API_KEY", "")

class Lotte(PayBot):
    """A narrative oriented GPT chatbot with semipersistent memory"""

    bios = {"lotte":("The following is a conversation with an AI assistant named Lotte. "
                "Lotte is a young girl who is helpful, creative, clever, funny, very friendly, a writer and anarcho-communist. Lotte's older sister is called Imogen and she's an artist. Lotte's best friend is Rowena the faerie.\n\n"
                "SOURCE: Hello, who are you?\nLotte: My name is Lotte, I'm an AI that loves having rivetting intellectual discussions. How can I help you today?"
                # "SOURCE: MESSAGE \nLotte: "
            ),"rowena":("The following is a conversation with a faerie named Rowena. "
                "Rowena is a faerie with mushroom like appearance, gossamer wings and a pointy tail."
                "She likes to play tricks on people, and whilst she might come across as mean she really wants everyone to have fun. Rowena's best friend is Lotte.\n\n"
                "SOURCE: Hello, who are you?\nRowena: My name is Rowena, I'm a faerie sort of creature, what sort of creature are you?"
                # "SOURCE: MESSAGE \nRowena: "
            ),
            }

    current_fronter="lotte"

    conversation={}

    # 0 means it's free, 1 means it's occupied
    namepool = {"Taylor":0,
                "Morgan":0,
                "Lee":0,
                "Rhianon":0,
                "Georgie": 0,
                "Moritz": 0
                }
    
    
    async def chat(self, msg: Message, fronter: str) -> Response:
        """ Organises the conversation and sends responses """

        prompt = self.bios[fronter].replace("SOURCE",msg.source)
        for line in self.conversation:
            prompt= prompt + "\n" + line
        prompt = prompt + "\n" + f"{msg.source}: {msg.text} \n{fronter}: "
        self.conversation[time.time()]=f"{msg.source}: {msg.text}"
        
        result = openai.Completion.create(  # type: ignore
            engine="curie",
            prompt = prompt,
            temperature=0.75,
            max_tokens=250,
            top_p=1,
            frequency_penalty=0.01,
            presence_penalty=0.7,
            stop=["\n", f"{msg.source}:", fronter],
        )
        answer= result["choices"][0]["text"].strip().replace("AI:", "\nAI:").replace(f"{msg.source}:", f"\n{msg.source}:")

        fronter = fronter[0].upper()+fronter[1:] #names being properly uppercase might help the prompt

        self.conversation[time.time()] = answer
        logging.info("conversation:")
        for line in self.conversation:
            logging.info(line)

        return answer # + f"\n and here's the extra:\n {str(len(self.conversation))} \n {self.conversation[len(self.conversation)-1]}"

        


    async def do_rowena(self,msg:Message) -> Response:
        """Chat With Rowena the Faerie"""
        return await self.chat(msg,"rowena")

    async def do_lotte(self,msg:Message) -> Response:
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
        self.conversation={}
        return "conversation history has been cleared"

    # async def summarize_history(self, conversation) -> list(str):
    #     openai.
        
        
    #     return conversation

    

    async def default(self, msg: Message) -> Response:

        

        if msg.arg0 or msg.full_text:
            msg.text = msg.full_text
            return await self.chat(msg,self.current_fronter)
        return await super().default(msg)


if __name__ == "__main__":
    run_bot(Lotte)