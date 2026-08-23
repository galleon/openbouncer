from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FunctionDefinition(BaseModel):
    """One entry of a request's `tools` list -- the client-declared
    signature of a function the model may choose to call. `parameters` is
    an arbitrary JSON Schema object (its own internal shape isn't this
    gateway's concern to validate); OpenBouncer relays it upstream
    unmodified, the same as every other pass-through request field."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    parameters: dict | None = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: FunctionDefinition


class ToolChoiceFunctionName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class NamedToolChoice(BaseModel):
    """The object form of `tool_choice` -- forces the model to call one
    specific, named function, as opposed to the string forms ("auto" /
    "none" / "required")."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: ToolChoiceFunctionName


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # A JSON-encoded string (the model's own serialization, not yet
    # parsed/validated against `parameters`) -- matches the wire format
    # OpenAI-compatible upstreams actually send. Parsing/validating it is
    # the calling client's responsibility, same as OpenAI's own contract;
    # OpenBouncer relays it as opaque text.
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["function"]
    function: FunctionCall


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
    # None only for an assistant message that's *entirely* a tool call
    # (matches the real OpenAI-compatible wire format for replaying prior
    # tool-calling turns back into message history: `content: null,
    # tool_calls: [...]`) -- see _validate_tool_calling_fields below, which
    # still requires content for every other role.
    content: str | list[ContentPart] | None = None
    # Only ever set on an assistant message (a model's own prior turn that
    # chose to call one or more tools) -- see _validate_tool_calling_fields.
    tool_calls: list[ToolCall] | None = None
    # Set when this message is a tool's result being fed back to the model
    # (role="tool"), correlating it to the ToolCall.id it's answering.
    # Deliberately not required/validated against role="tool" here --
    # OpenBouncer has always accepted bare role="tool" messages without it
    # (pre-dating tool-calling support in this schema), so this stays
    # permissive rather than retroactively tightening that.
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _validate_tool_calling_fields(self) -> "ChatMessage":
        if self.tool_calls is not None and self.role != "assistant":
            raise ValueError("tool_calls is only valid on assistant messages")
        if self.content is None and self.role != "assistant":
            raise ValueError(f"content is required for {self.role!r} messages")
        return self


class ResponseChatMessage(BaseModel):
    """An assistant message on a chat completion *response* -- unlike
    ChatMessage (used to validate client *requests*), this deliberately
    has no extra="forbid": real upstream providers routinely include
    vendor extension fields on response messages (e.g. vLLM's
    `reasoning`, `refusal`) that this gateway doesn't define or use, and
    rejecting the whole response over fields we don't care about would be
    wrong.
    """

    role: Literal["system", "user", "assistant", "tool"]
    # None when the response is entirely a tool call -- see ChatMessage's
    # matching field. No validator here (unlike ChatMessage): this model
    # validates upstream *output*, which this gateway doesn't get to
    # reject just because some other field looks inconsistent -- see the
    # class docstring.
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None


class GuardrailsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    config_id: str | None = None
    # Accepted for UI/reproducibility purposes (e.g. "which example scenario
    # was this") -- does not currently change which rails run. See
    # app.guardrails.catalog for the fixed set of presets the UI offers.
    preset: str | None = None


class StreamOptions(BaseModel):
    """OpenAI's `stream_options` -- currently just `include_usage`, which
    asks for one extra final SSE chunk (empty `choices`, a top-level
    `usage` object) right before `[DONE]`. See
    UpstreamClient.stream_chat_completion, which always sets this on the
    *upstream* request regardless of what the caller of this gateway asked
    for (so OpenBouncer can capture real usage for its own accounting),
    but only relays that extra chunk back to the caller if they opted in
    here -- matching what a client library actually expects to receive.
    """

    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=1.0, ge=0, le=2)
    top_p: float | None = Field(default=1.0, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool | None = False
    stream_options: StreamOptions | None = None
    stop: str | list[str] | None = None
    user: str | None = None
    guardrails: GuardrailsOptions | None = None
    tools: list[Tool] | None = None
    tool_choice: Literal["none", "auto", "required"] | NamedToolChoice | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ResponseChatMessage
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
