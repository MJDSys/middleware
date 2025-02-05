import aiohttp
import aiohttp.client_exceptions
import asyncio
import async_timeout
import contextlib
import os
import typing
import urllib.parse

from .config import get_config
from .exceptions import ApiException
from .utils import RequestMode, UPDATE_HEADERS


class ClientMixin:

    @classmethod
    @contextlib.asynccontextmanager
    async def request(
        cls, endpoint: str, mode: str, body: typing.Any = None, headers: typing.Optional[dict] = None,
        timeout: int = 50, handle_timeout: bool = True,
    ) -> aiohttp.ClientResponse:
        exceptions = [aiohttp.ClientResponseError] + ([asyncio.TimeoutError] if handle_timeout else [])
        try:
            async with async_timeout.timeout(timeout):
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=get_config().ssl_context)
                ) as session:
                    async with await getattr(session, mode)(
                        urllib.parse.urljoin(get_config().server, endpoint), json=body, headers=headers
                    ) as resp:
                        if resp.status not in (200, 201):
                            raise ApiException(f'Received {resp.status!r} response code from {endpoint!r}')

                        yield resp
        except tuple(exceptions) as e:
            raise ApiException(f'Failed {endpoint!r} call: {e!r}')

    @classmethod
    async def api_call(
        cls, endpoint: str, mode: str, body: typing.Any = None, headers: typing.Optional[dict] = None,
        response_type: str = 'json', timeout: int = 50
    ) -> typing.Union[dict, str]:
        try:
            async with cls.request(endpoint, mode, body, headers, timeout) as resp:
                return await resp.json() if response_type == 'json' else await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientResponseError) as e:
            raise ApiException(f'Failed {endpoint!r} call: {e!r}')
        except aiohttp.client_exceptions.ContentTypeError as e:
            raise ApiException(f'Malformed response received from {endpoint!r} endpoint: {e}')


class K8sClientBase(ClientMixin):

    NAMESPACE: str = NotImplementedError
    OBJECT_ENDPOINT: str = NotImplementedError
    OBJECT_HUMAN_NAME: str = NotImplementedError
    OBJECT_TYPE: str = NotImplementedError

    @classmethod
    def query_selectors(cls, parameters: typing.Optional[dict]) -> str:
        return f'?{urllib.parse.urlencode(parameters)}' if parameters else ''

    @classmethod
    def uri(
        cls, namespace: typing.Optional[str] = None, object_name: typing.Optional[str] = None,
        parameters: typing.Optional[dict] = None,
    ) -> str:
        """
        Kubernetes API URI docs ( https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-uris )

        Based on namespace we deduce the URI going to be used for K8s resource.
        """
        return (os.path.join(
            cls.NAMESPACE, namespace, cls.OBJECT_TYPE, *([object_name] if object_name else [])
        ) if namespace else os.path.join(cls.OBJECT_ENDPOINT, *(
            [object_name] if object_name else []
        ))) + cls.query_selectors(parameters)

    @classmethod
    async def call(
        cls, uri: str, mode: str, body: typing.Any = None, headers: typing.Optional[dict] = None, **kwargs
    ):
        return await cls.api_call(uri, mode, body, headers, **kwargs)

    @classmethod
    async def get_instance(cls, name: str, **kwargs) -> dict:
        instance = await cls.query(
            fieldSelector=f'metadata.name={name}', request_kwargs=kwargs.pop('request_kwargs', None)
        )
        if not instance.get('items'):
            raise ApiException(f'Unable to find "{name!r}" {cls.OBJECT_HUMAN_NAME}')
        else:
            return instance['items'][0]

    @classmethod
    async def query(cls, *args, **kwargs):
        request_kwargs = kwargs.pop('request_kwargs', None) or {}
        return await cls.call(
            cls.uri(namespace=kwargs.pop('namespace', None), parameters=kwargs),
            mode=RequestMode.GET.value, **request_kwargs,
        )

    @classmethod
    async def create(cls, data: dict, **kwargs):
        return await cls.call(cls.uri(
            namespace=kwargs.pop('namespace', None), parameters=kwargs,
        ), body=data, mode=RequestMode.POST.value)

    @classmethod
    async def update(cls, name: str, data: dict, **kwargs):
        return await cls.call(cls.uri(
            namespace=kwargs.pop('namespace', None), parameters=kwargs, object_name=name,
        ), body=data, mode=RequestMode.PATCH.value, headers=UPDATE_HEADERS)

    @classmethod
    async def delete(cls, name: str, **kwargs):
        return await cls.call(cls.uri(
            object_name=name, namespace=kwargs.pop('namespace', None), parameters=kwargs,
        ), mode=RequestMode.DELETE.value)
