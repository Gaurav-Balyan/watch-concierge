"""
List the model ids your Groq key can use (for GROQ_MODEL and ROUTER_MODEL in .env).

    python -m tools.groq_models
"""
import asyncio

from groq import AsyncGroq

from common import config


async def list_models():
    models = await AsyncGroq().models.list()
    for m in sorted(models.data, key=lambda m: m.id):
        print(m.id)


if __name__ == "__main__":
    config.require("GROQ_API_KEY")
    asyncio.run(list_models())
