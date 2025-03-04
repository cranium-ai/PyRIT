# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import logging
from httpx import HTTPStatusError
from typing import Optional

from pyrit.chat_message_normalizer import ChatMessageNop, ChatMessageNormalizer
from pyrit.common import default_values, net_utility
from pyrit.exceptions import EmptyResponseException, RateLimitException
from pyrit.exceptions import handle_bad_request_exception, pyrit_target_retry
from pyrit.memory import MemoryInterface
from pyrit.models import ChatMessage, PromptRequestResponse
from pyrit.models import construct_response_from_request
from pyrit.prompt_target import PromptChatTarget, limit_requests_per_minute

logger = logging.getLogger(__name__)


class AzureMLChatTarget(PromptChatTarget):

    API_KEY_ENVIRONMENT_VARIABLE: str = "AZURE_ML_KEY"
    ENDPOINT_URI_ENVIRONMENT_VARIABLE: str = "AZURE_ML_MANAGED_ENDPOINT"

    def __init__(
        self,
        *,
        endpoint_uri: str = None,
        api_key: str = None,
        chat_message_normalizer: ChatMessageNormalizer = ChatMessageNop(),
        memory: MemoryInterface = None,
        max_tokens: int = 400,
        temperature: float = 1.0,
        top_p: int = 1,
        repetition_penalty: float = 1.2,
        max_requests_per_minute: Optional[int] = None,
    ) -> None:
        """
        Initializes an instance of the AzureMLChatTarget class.

        Args:
            endpoint_uri (str, optional): The URI of the Azure ML endpoint.
                Defaults to None.
            api_key (str, optional): The API key for accessing the Azure ML endpoint.
                Defaults to None.
            chat_message_normalizer (ChatMessageNormalizer, optional): The chat message normalizer.
                Defaults to ChatMessageNop().
            memory (MemoryInterface, optional): The memory interface.
                Defaults to None.
            max_tokens (int, optional): The maximum number of tokens to generate in the response.
                Defaults to 400.
            temperature (float, optional): The temperature for generating diverse responses.
                Defaults to 1.0.
            top_p (int, optional): The top-p value for generating diverse responses.
                Defaults to 1.
            repetition_penalty (float, optional): The repetition penalty for generating diverse responses.
                Defaults to 1.2.
            max_requests_per_minute (int, optional): Number of requests the target can handle per
                minute before hitting a rate limit. The number of requests sent to the target
                will be capped at the value provided.
        """
        PromptChatTarget.__init__(self, memory=memory, max_requests_per_minute=max_requests_per_minute)

        self.endpoint_uri: str = default_values.get_required_value(
            env_var_name=self.ENDPOINT_URI_ENVIRONMENT_VARIABLE, passed_value=endpoint_uri
        )
        self.api_key: str = default_values.get_required_value(
            env_var_name=self.API_KEY_ENVIRONMENT_VARIABLE, passed_value=api_key
        )
        self.chat_message_normalizer = chat_message_normalizer

        self._max_tokens = max_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._repetition_penalty = repetition_penalty

    @limit_requests_per_minute
    async def send_prompt_async(self, *, prompt_request: PromptRequestResponse) -> PromptRequestResponse:

        self._validate_request(prompt_request=prompt_request)
        request = prompt_request.request_pieces[0]

        messages = self._memory.get_chat_messages_with_conversation_id(conversation_id=request.conversation_id)

        messages.append(request.to_chat_message())

        logger.info(f"Sending the following prompt to the prompt target: {request}")

        try:
            resp_text = await self._complete_chat_async(
                messages=messages,
                temperature=self._temperature,
                top_p=self._top_p,
                repetition_penalty=self._repetition_penalty,
            )

            if not resp_text:
                raise EmptyResponseException(message="The chat returned an empty response.")

            response_entry = construct_response_from_request(request=request, response_text_pieces=[resp_text])
        except HTTPStatusError as hse:
            if hse.response.status_code == 400:
                # Handle Bad Request
                response_entry = handle_bad_request_exception(response_text=hse.response.text, request=request)
            elif hse.response.status_code == 429:
                raise RateLimitException()
            else:
                raise hse

        logger.info(
            "Received the following response from the prompt target"
            + f"{response_entry.request_pieces[0].converted_value}"
        )
        return response_entry

    @pyrit_target_retry
    async def _complete_chat_async(
        self,
        messages: list[ChatMessage],
        max_tokens: int = 400,
        temperature: float = 1.0,
        top_p: int = 1,
        repetition_penalty: float = 1.2,
    ) -> str:
        """
        Completes a chat interaction by generating a response to the given input prompt.

        This is a synchronous wrapper for the asynchronous _generate_and_extract_response method.

        Args:
            messages (list[ChatMessage]): The chat messages objects containing the role and content.
            max_tokens (int, optional): The maximum number of tokens to generate. Defaults to 400.
            temperature (float, optional): Controls randomness in the response generation. Defaults to 1.0.
                1 is more random, 0 is less.
            top_p (int, optional): Controls diversity of the response generation. Defaults to 1.
                1 is more random, 0 is less.
            repetition_penalty (float, optional): Controls repetition in the response generation.
                Defaults to 1.2.

        Raises:
            Exception: For any errors during the process.

        Returns:
            str: The generated response message.
        """
        headers = self._get_headers()
        payload = self._construct_http_body(messages, max_tokens, temperature, top_p, repetition_penalty)

        response = await net_utility.make_request_and_raise_if_error_async(
            endpoint_uri=self.endpoint_uri, method="POST", request_body=payload, headers=headers
        )

        return response.json()["output"]

    def _construct_http_body(
        self,
        messages: list[ChatMessage],
        max_tokens: int,
        temperature: float,
        top_p: int,
        repetition_penalty: float,
    ) -> dict:
        """Constructs the HTTP request body for the AML online endpoint."""

        squashed_messages = self.chat_message_normalizer.normalize(messages)
        messages_dict = [message.model_dump() for message in squashed_messages]
        data = {
            "input_data": {
                "input_string": messages_dict,
                "parameters": {
                    "max_new_tokens": max_tokens,
                    "temperature": temperature,
                    "top_p": top_p,
                    "top_k": 50,
                    "stop": ["</s>"],
                    "stop_sequences": ["</s>"],
                    "return_full_text": False,
                    "repetition_penalty": repetition_penalty,
                },
            }
        }
        return data

    def _get_headers(self) -> dict:
        """Headers for accessing inference endpoint deployed in AML.
        Returns:
            headers(dict): contains bearer token as AML key and content-type: JSON
        """

        headers: dict = {
            "Content-Type": "application/json",
            "Authorization": ("Bearer " + self.api_key),
        }

        return headers

    def _validate_request(self, *, prompt_request: PromptRequestResponse) -> None:
        if len(prompt_request.request_pieces) != 1:
            raise ValueError("This target only supports a single prompt request piece.")

        if prompt_request.request_pieces[0].converted_value_data_type != "text":
            raise ValueError("This target only supports text prompt input.")
