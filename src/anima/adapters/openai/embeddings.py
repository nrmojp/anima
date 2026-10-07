"""General OpenAI implementation of the embedding provider port."""

from openai import AsyncOpenAI


class OpenAIEmbeddingProvider:
    def __init__(self, client: AsyncOpenAI, *, model: str = "text-embedding-3-small", dimensions: int = 512):
        self.client = client
        self.model = model
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self.client.embeddings.create(
            model=self.model, input=texts, dimensions=self.dimensions, encoding_format="float",
        )
        return [list(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]
