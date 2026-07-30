from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    detail: Literal["auto", "low", "high"] | None = "auto"


class TextContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ImageContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Annotated[Union[TextContentPart, ImageContentPart], Field(discriminator="type")]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart]


class GuardrailsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    config_id: str | None = None
    # Accepted for UI/reproducibility purposes (e.g. "which example scenario
    # was this") -- does not currently change which rails run. See
    # app.guardrails.catalog for the fixed set of presets the UI offers.
    preset: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=1.0, ge=0, le=2)
    top_p: float | None = Field(default=1.0, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool | None = False
    stop: str | list[str] | None = None
    user: str | None = None
    guardrails: GuardrailsOptions | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
