from __future__ import annotations

import time
import random
import string
import logging
import asyncio
from typing import Union, AsyncIterator, Coroutine
from ..providers.base_provider import AsyncGeneratorProvider
from ..image import ImageResponse
from ..typing import Messages
from ..providers.types import ProviderType, FinishReason
from ..providers.conversation import BaseConversation
from ..image import ImageResponse as ImageProviderResponse
from .stubs import ChatCompletion, ChatCompletionChunk, Image, ImagesResponse
from .image_models import ImageModels
from .types import ImageProvider
from .types import Client as BaseClient
from .service import get_model_and_provider, get_last_provider
from .helper import find_stop, filter_json, filter_none
from .helper import cast_iter_async
from .client import Images as BaseImages

try:
    anext # Python 3.8+
except NameError:
    async def anext(aiter):
        try:
            return await aiter.__anext__()
        except StopAsyncIteration:
            raise StopIteration

async def safe_aclose(generator):
    try:
        await generator.aclose()
    except Exception as e:
        logging.warning(f"Error while closing generator: {e}")

async def iter_response(
    response: AsyncIterator[str],
    stream: bool,
    response_format: dict = None,
    max_tokens: int = None,
    stop: list = None
) -> AsyncIterator[Union[ChatCompletion, ChatCompletionChunk]]:
    content = ""
    finish_reason = None
    completion_id = ''.join(random.choices(string.ascii_letters + string.digits, k=28))
    idx = 0

    try:
        async for chunk in response:
            if isinstance(chunk, FinishReason):
                finish_reason = chunk.reason
                break
            elif isinstance(chunk, BaseConversation):
                yield chunk
                continue

            content += str(chunk)
            idx += 1

            if max_tokens is not None and idx >= max_tokens:
                finish_reason = "length"

            first, content, chunk = find_stop(stop, content, chunk if stream else None)

            if first != -1:
                finish_reason = "stop"

            if stream:
                yield ChatCompletionChunk(chunk, None, completion_id, int(time.time()))

            if finish_reason is not None:
                break

        finish_reason = "stop" if finish_reason is None else finish_reason

        if stream:
            yield ChatCompletionChunk(None, finish_reason, completion_id, int(time.time()))
        else:
            if response_format is not None and "type" in response_format:
                if response_format["type"] == "json_object":
                    content = filter_json(content)
            yield ChatCompletion(content, finish_reason, completion_id, int(time.time()))
    finally:
        if hasattr(response, 'aclose'):
            await safe_aclose(response)

async def iter_append_model_and_provider(response: AsyncIterator) -> AsyncIterator:
    last_provider = None
    try:
        async for chunk in response:
            last_provider = get_last_provider(True) if last_provider is None else last_provider
            chunk.model = last_provider.get("model")
            chunk.provider = last_provider.get("name")
            yield chunk
    finally:
        if hasattr(response, 'aclose'):
            await safe_aclose(response)

class AsyncClient(BaseClient):
    def __init__(
        self,
        provider: ProviderType = None,
        image_provider: ImageProvider = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.chat: Chat = Chat(self, provider)
        self._images: Images = Images(self, image_provider)

    @property
    def images(self) -> Images:
        return self._images

class Completions:
    def __init__(self, client: 'AsyncClient', provider: ProviderType = None):
        self.client: 'AsyncClient' = client
        self.provider: ProviderType = provider

    def create(
        self,
        messages: Messages,
        model: str,
        provider: ProviderType = None,
        stream: bool = False,
        proxy: str = None,
        response_format: dict = None,
        max_tokens: int = None,
        stop: Union[list[str], str] = None,
        api_key: str = None,
        ignored: list[str] = None,
        ignore_working: bool = False,
        ignore_stream: bool = False,
        **kwargs
    ) -> Union[Coroutine[ChatCompletion], AsyncIterator[ChatCompletionChunk]]:
        model, provider = get_model_and_provider(
            model,
            self.provider if provider is None else provider,
            stream,
            ignored,
            ignore_working,
            ignore_stream,
        )

        stop = [stop] if isinstance(stop, str) else stop

        response = provider.create_completion(
            model,
            messages,
            stream=stream,
            **filter_none(
                proxy=self.client.get_proxy() if proxy is None else proxy,
                max_tokens=max_tokens,
                stop=stop,
                api_key=self.client.api_key if api_key is None else api_key
            ),
            **kwargs
        )

        if isinstance(response, AsyncIterator):
            response = iter_response(response, stream, response_format, max_tokens, stop)
            response = iter_append_model_and_provider(response)
            return response if stream else anext(response)
        else:
            response = cast_iter_async(response)
            response = iter_response(response, stream, response_format, max_tokens, stop)
            response = iter_append_model_and_provider(response)
            return response if stream else anext(response)

class Chat:
    completions: Completions

    def __init__(self, client: AsyncClient, provider: ProviderType = None):
        self.completions = Completions(client, provider)

async def iter_image_response(response: AsyncIterator) -> Union[ImagesResponse, None]:
    logging.info("Starting iter_image_response")
    try:
        async for chunk in response:
            logging.info(f"Processing chunk: {chunk}")
            if isinstance(chunk, ImageProviderResponse):
                logging.info("Found ImageProviderResponse")
                return ImagesResponse([Image(image) for image in chunk.get_list()])
        
        logging.warning("No ImageProviderResponse found in the response")
        return None
    finally:
        if hasattr(response, 'aclose'):
            await safe_aclose(response)

async def create_image(client: AsyncClient, provider: ProviderType, prompt: str, model: str = "", **kwargs) -> AsyncIterator:
    logging.info(f"Creating image with provider: {provider}, model: {model}, prompt: {prompt}")

    if isinstance(provider, type) and provider.__name__ == "You":
        kwargs["chat_mode"] = "create"
    else:
        prompt = f"create an image with: {prompt}"
    
    response = await provider.create_completion(
        model,
        [{"role": "user", "content": prompt}],
        stream=True,
        proxy=client.get_proxy(),
        **kwargs
    )
    
    logging.info(f"Response from create_completion: {response}")
    return response

class Images(BaseImages):
    def __init__(self, client: 'AsyncClient', provider: ImageProvider = None):
        self.client: 'AsyncClient' = client
        self.provider: ImageProvider = provider
        self.models: ImageModels = ImageModels(client)

    async def generate(self, prompt: str, model: str = None, response_format: str = "url", **kwargs) -> ImagesResponse:
        return await self.async_generate(prompt, model, response_format, **kwargs)
    
    async def create_variation(self, image: Union[str, bytes], model: str = None, **kwargs) -> ImagesResponse:
        provider = self.models.get(model, self.provider)
        if provider is None:
            raise ValueError(f"Unknown model: {model}")

        if isinstance(provider, type) and issubclass(provider, AsyncGeneratorProvider):
            messages = [{"role": "user", "content": "create a variation of this image"}]
            image_data = to_data_uri(image)
            generator = None
            try:
                generator = provider.create_async_generator(model, messages, image=image_data, **kwargs)
                async for response in generator:
                    if isinstance(response, ImageResponse):
                        return self._process_image_response(response)
                    elif isinstance(response, str):
                        image_response = ImageResponse([response], "Image variation")
                        return self._process_image_response(image_response)
            except RuntimeError as e:
                if "async generator ignored GeneratorExit" in str(e):
                    logging.warning("Generator ignored GeneratorExit in create_variation, handling gracefully")
                else:
                    raise
            finally:
                if generator and hasattr(generator, 'aclose'):
                    await safe_aclose(generator)
                logging.info("AsyncGeneratorProvider processing completed in create_variation")
        elif hasattr(provider, 'create_variation'):
            if asyncio.iscoroutinefunction(provider.create_variation):
                response = await provider.create_variation(image, **kwargs)
            else:
                response = provider.create_variation(image, **kwargs)

            if isinstance(response, ImageResponse):
                return self._process_image_response(response)
            elif isinstance(response, str):
                image_response = ImageResponse([response], "Image variation")
                return self._process_image_response(image_response)
        else:
            raise ValueError(f"Provider {provider} does not support image variation")