from __future__ import annotations

import inspect
import logging
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, Optional

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.llms import LLM
from langchain_core.outputs import GenerationChunk
from langchain_core.utils import from_env, get_pydantic_field_names
from pydantic import ConfigDict, Field, model_validator
from typing_extensions import Self

logger = logging.getLogger(__name__)

VALID_TASKS = (
    "text2text-generation",
    "text-generation",
    "summarization",
    "conversational",
)


class HuggingFaceEndpoint(LLM):
    """Hugging Face Endpoint. This works with any model that supports text generation (i.e. text completion) task.

    To use this class, you should have installed the ``huggingface_hub`` package, and
    the environment variable ``HUGGINGFACEHUB_API_TOKEN`` set with your API token,
    or given as a named parameter to the constructor.

    Example:
        .. code-block:: python

            # Basic Example (no streaming)
            llm = HuggingFaceEndpoint(
                endpoint_url="http://localhost:8010/",
                max_new_tokens=512,
                top_k=10,
                top_p=0.95,
                typical_p=0.95,
                temperature=0.01,
                repetition_penalty=1.03,
                huggingfacehub_api_token="my-api-key"
            )
            print(llm.invoke("What is Deep Learning?"))

            # Streaming response example
            from langchain_core.callbacks.streaming_stdout import StreamingStdOutCallbackHandler

            callbacks = [StreamingStdOutCallbackHandler()]
            llm = HuggingFaceEndpoint(
                endpoint_url="http://localhost:8010/",
                max_new_tokens=512,
                top_k=10,
                top_p=0.95,
                typical_p=0.95,
                temperature=0.01,
                repetition_penalty=1.03,
                callbacks=callbacks,
                streaming=True,
                huggingfacehub_api_token="my-api-key"
            )
            print(llm.invoke("What is Deep Learning?"))

            # Basic Example (no streaming) with Mistral-Nemo-Base-2407 model using a third-party provider (Novita).
            llm = HuggingFaceEndpoint(
                repo_id="mistralai/Mistral-Nemo-Base-2407",
                provider="novita",
                max_new_tokens=100,
                do_sample=False,
                huggingfacehub_api_token="my-api-key"
            )
            print(llm.invoke("What is Deep Learning?"))

    """  # noqa: E501

    endpoint_url: Optional[str] = None
    """Endpoint URL to use. If repo_id is not specified then this needs to given or
    should be pass as env variable in `HF_INFERENCE_ENDPOINT`"""
    repo_id: Optional[str] = None
    """Repo to use. If endpoint_url is not specified then this needs to given"""
    provider: Optional[str] = None
    """Name of the provider to use for inference with the model specified in `repo_id`.
        e.g. "cerebras". if not specified, Defaults to "auto" i.e. the first of the
        providers available for the model, sorted by the user's order in https://hf.co/settings/inference-providers.
        available providers can be found in the [huggingface_hub documentation](https://huggingface.co/docs/huggingface_hub/guides/inference#supported-providers-and-tasks)."""
    huggingfacehub_api_token: Optional[str] = Field(
        default_factory=from_env("HUGGINGFACEHUB_API_TOKEN", default=None)
    )
    max_new_tokens: int = 512
    """Maximum number of generated tokens"""
    top_k: Optional[int] = None
    """The number of highest probability vocabulary tokens to keep for
    top-k-filtering."""
    top_p: Optional[float] = 0.95
    """If set to < 1, only the smallest set of most probable tokens with probabilities
    that add up to `top_p` or higher are kept for generation."""
    typical_p: Optional[float] = 0.95
    """Typical Decoding mass. See [Typical Decoding for Natural Language
    Generation](https://arxiv.org/abs/2202.00666) for more information."""
    temperature: Optional[float] = 0.8
    """The value used to module the logits distribution."""
    repetition_penalty: Optional[float] = None
    """The parameter for repetition penalty. 1.0 means no penalty.
    See [this paper](https://arxiv.org/pdf/1909.05858.pdf) for more details."""
    return_full_text: bool = False
    """Whether to prepend the prompt to the generated text"""
    truncate: Optional[int] = None
    """Truncate inputs tokens to the given size"""
    stop_sequences: list[str] = Field(default_factory=list)
    """Stop generating tokens if a member of `stop_sequences` is generated"""
    seed: Optional[int] = None
    """Random sampling seed"""
    inference_server_url: str = ""
    """text-generation-inference instance base url"""
    timeout: int = 120
    """Timeout in seconds"""
    streaming: bool = False
    """Whether to generate a stream of tokens asynchronously"""
    do_sample: bool = False
    """Activate logits sampling"""
    watermark: bool = False
    """Watermarking with [A Watermark for Large Language Models]
    (https://arxiv.org/abs/2301.10226)"""
    server_kwargs: dict[str, Any] = Field(default_factory=dict)
    """Holds any text-generation-inference server parameters not explicitly specified"""
    model_kwargs: dict[str, Any] = Field(default_factory=dict)
    """Holds any model parameters valid for `call` not explicitly specified"""
    model: str
    client: Any = None  #: :meta private:
    async_client: Any = None  #: :meta private:
    task: Optional[str] = None
    """Task to call the model with. Should be a task that returns `generated_text`."""

    model_config = ConfigDict(
        extra="forbid",
    )

    @model_validator(mode="before")
    @classmethod
    def build_extra(cls, values: dict[str, Any]) -> Any:
        """Build extra kwargs from additional params that were passed in."""
        all_required_field_names = get_pydantic_field_names(cls)
        extra = values.get("model_kwargs", {})
        for field_name in list(values):
            if field_name in extra:
                msg = f"Found {field_name} supplied twice."
                raise ValueError(msg)
            if field_name not in all_required_field_names:
                logger.warning(
                    f"""WARNING! {field_name} is not default parameter.
                    {field_name} was transferred to model_kwargs.
                    Please make sure that {field_name} is what you intended."""
                )
                extra[field_name] = values.pop(field_name)

        invalid_model_kwargs = all_required_field_names.intersection(extra.keys())
        if invalid_model_kwargs:
            msg = (
                f"Parameters {invalid_model_kwargs} should be specified explicitly. "
                f"Instead they were passed in as part of `model_kwargs` parameter."
            )
            raise ValueError(msg)

        values["model_kwargs"] = extra

        # to correctly create the InferenceClient and AsyncInferenceClient
        # in validate_environment, we need to populate values["model"].
        # from InferenceClient docstring:
        # model (`str`, `optional`):
        #     The model to run inference with. Can be a model id hosted on the Hugging
        #       Face Hub, e.g. `bigcode/starcoder`
        #     or a URL to a deployed Inference Endpoint. Defaults to None, in which
        #       case a recommended model is
        #     automatically selected for the task.

        # this string could be in 3 places of descending priority:
        # 2. values["model"] or values["endpoint_url"] or values["repo_id"]
        #       (equal priority - don't allow both set)
        # 3. values["HF_INFERENCE_ENDPOINT"] (if none above set)

        model = values.get("model")
        endpoint_url = values.get("endpoint_url")
        repo_id = values.get("repo_id")

        if sum([bool(model), bool(endpoint_url), bool(repo_id)]) > 1:
            msg = (
                "Please specify either a `model` OR an `endpoint_url` OR a `repo_id`,"
                "not more than one."
            )
            raise ValueError(msg)
        values["model"] = (
            model or endpoint_url or repo_id or os.environ.get("HF_INFERENCE_ENDPOINT")
        )
        if not values["model"]:
            msg = (
                "Please specify a `model` or an `endpoint_url` or a `repo_id` for the "
                "model."
            )
            raise ValueError(msg)
        return values

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        """Validate that package is installed and that the API token is valid."""
        huggingfacehub_api_token = self.huggingfacehub_api_token or os.getenv(
            "HF_TOKEN"
        )

        from huggingface_hub import (  # type: ignore[import]
            AsyncInferenceClient,  # type: ignore[import]
            InferenceClient,  # type: ignore[import]
        )

        # Instantiate clients with supported kwargs
        sync_supported_kwargs = set(inspect.signature(InferenceClient).parameters)
        self.client = InferenceClient(
            model=self.model,
            timeout=self.timeout,
            api_key=huggingfacehub_api_token,
            provider=self.provider,  # type: ignore[arg-type]
            **{
                key: value
                for key, value in self.server_kwargs.items()
                if key in sync_supported_kwargs
            },
        )

        async_supported_kwargs = set(inspect.signature(AsyncInferenceClient).parameters)
        self.async_client = AsyncInferenceClient(
            model=self.model,
            timeout=self.timeout,
            api_key=huggingfacehub_api_token,
            provider=self.provider,  # type: ignore[arg-type]
            **{
                key: value
                for key, value in self.server_kwargs.items()
                if key in async_supported_kwargs
            },
        )
        ignored_kwargs = (
            set(self.server_kwargs.keys())
            - sync_supported_kwargs
            - async_supported_kwargs
        )
        if len(ignored_kwargs) > 0:
            logger.warning(
                f"Ignoring following parameters as they are not supported by the "
                f"InferenceClient or AsyncInferenceClient: {ignored_kwargs}."
            )

        return self

    @property
    def _default_params(self) -> dict[str, Any]:
        """Get the default parameters for calling text generation inference API."""
        return {
            "max_new_tokens": self.max_new_tokens,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "typical_p": self.typical_p,
            "temperature": self.temperature,
            "repetition_penalty": self.repetition_penalty,
            "return_full_text": self.return_full_text,
            "truncate": self.truncate,
            "stop": self.stop_sequences,
            "seed": self.seed,
            "do_sample": self.do_sample,
            "watermark": self.watermark,
            **self.model_kwargs,
        }

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        _model_kwargs = self.model_kwargs or {}
        return {
            "endpoint_url": self.endpoint_url,
            "task": self.task,
            "provider": self.provider,
            "model_kwargs": _model_kwargs,
        }

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "huggingface_endpoint"

    def _invocation_params(
        self, runtime_stop: Optional[list[str]], **kwargs: Any
    ) -> dict[str, Any]:
        params = {**self._default_params, **kwargs}
        params["stop"] = params["stop"] + (runtime_stop or [])
        return params

    def _call(
        self,
        prompt: str,
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call out to HuggingFace Hub's inference endpoint."""
        invocation_params = self._invocation_params(stop, **kwargs)
        if self.streaming:
            completion = ""
            for chunk in self._stream(
                prompt, run_manager=run_manager, **invocation_params
            ):
                completion += chunk.text
            return completion

        response_text = self.client.text_generation(
            prompt=prompt,
            model=self.model,
            **invocation_params,
        )

        # Maybe the generation has stopped at one of the stop sequences:
        # then we remove this stop sequence from the end of the generated text
        for stop_seq in invocation_params["stop"]:
            if response_text[-len(stop_seq) :] == stop_seq:
                response_text = response_text[: -len(stop_seq)]
        return response_text

    async def _acall(
        self,
        prompt: str,
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        invocation_params = self._invocation_params(stop, **kwargs)
        if self.streaming:
            completion = ""
            async for chunk in self._astream(
                prompt, run_manager=run_manager, **invocation_params
            ):
                completion += chunk.text
            return completion

        response_text = await self.async_client.text_generation(
            prompt=prompt,
            **invocation_params,
            model=self.model,
            stream=False,
        )

        # Maybe the generation has stopped at one of the stop sequences:
        # then remove this stop sequence from the end of the generated text
        for stop_seq in invocation_params["stop"]:
            if response_text[-len(stop_seq) :] == stop_seq:
                response_text = response_text[: -len(stop_seq)]
        return response_text

    def _stream(
        self,
        prompt: str,
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        invocation_params = self._invocation_params(stop, **kwargs)

        for response in self.client.text_generation(
            prompt, **invocation_params, stream=True
        ):
            # identify stop sequence in generated text, if any
            stop_seq_found: Optional[str] = None
            for stop_seq in invocation_params["stop"]:
                if stop_seq in response:
                    stop_seq_found = stop_seq

            # identify text to yield
            text: Optional[str] = None
            if stop_seq_found:
                text = response[: response.index(stop_seq_found)]
            else:
                text = response

            # yield text, if any
            if text:
                chunk = GenerationChunk(text=text)

                if run_manager:
                    run_manager.on_llm_new_token(chunk.text)
                yield chunk

            # break if stop sequence found
            if stop_seq_found:
                break

    async def _astream(
        self,
        prompt: str,
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenerationChunk]:
        invocation_params = self._invocation_params(stop, **kwargs)
        async for response in await self.async_client.text_generation(
            prompt, **invocation_params, stream=True
        ):
            # identify stop sequence in generated text, if any
            stop_seq_found: Optional[str] = None
            for stop_seq in invocation_params["stop"]:
                if stop_seq in response:
                    stop_seq_found = stop_seq

            # identify text to yield
            text: Optional[str] = None
            if stop_seq_found:
                text = response[: response.index(stop_seq_found)]
            else:
                text = response

            # yield text, if any
            if text:
                chunk = GenerationChunk(text=text)

                if run_manager:
                    await run_manager.on_llm_new_token(chunk.text)
                yield chunk

            # break if stop sequence found
            if stop_seq_found:
                break
