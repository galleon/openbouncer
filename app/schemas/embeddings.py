from pydantic import BaseModel


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage
