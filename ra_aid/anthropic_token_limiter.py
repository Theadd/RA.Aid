"""Utilities for handling token limits with Anthropic models."""

from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Union

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, trim_messages
from langchain_core.messages.base import message_to_dict
from langgraph.prebuilt.chat_agent_executor import AgentState
from litellm import token_counter

from ra_aid.agent_backends.ciayn_agent import CiaynAgent
from ra_aid.database.repositories.config_repository import get_config_repository
from ra_aid.logging_config import get_logger
from ra_aid.models_params import DEFAULT_TOKEN_LIMIT, models_params
from ra_aid.console.output import print_messages_compact

logger = get_logger(__name__)


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    """Helper function to estimate total tokens in a sequence of messages.

    Args:
        messages: Sequence of messages to count tokens for

    Returns:
        Total estimated token count
    """
    if not messages:
        return 0

    estimate_tokens = CiaynAgent._estimate_tokens
    return sum(estimate_tokens(msg) for msg in messages)


def convert_message_to_litellm_format(message: BaseMessage) -> Dict:
    """Convert a BaseMessage to the format expected by litellm.

    Args:
        message: The BaseMessage to convert

    Returns:
        Dict in litellm format
    """
    message_dict = message_to_dict(message)
    return {
        "role": message_dict["type"],
        "content": message_dict["data"]["content"],
    }


def create_token_counter_wrapper(model: str):
    """Create a wrapper for token counter that handles BaseMessage conversion.

    Args:
        model: The model name to use for token counting

    Returns:
        A function that accepts BaseMessage objects and returns token count
    """

    # Create a partial function that already has the model parameter set
    base_token_counter = partial(token_counter, model=model)

    def wrapped_token_counter(messages: List[BaseMessage]) -> int:
        """Count tokens in a list of messages, converting BaseMessage to dict for litellm token counter usage.

        Args:
            messages: List of BaseMessage objects

        Returns:
            Token count for the messages
        """
        if not messages:
            return 0

        litellm_messages = [convert_message_to_litellm_format(msg) for msg in messages]
        result = base_token_counter(messages=litellm_messages)
        return result

    return wrapped_token_counter


def state_modifier(
    state: AgentState, model: ChatAnthropic, max_input_tokens: int = DEFAULT_TOKEN_LIMIT
) -> list[BaseMessage]:
    """Given the agent state and max_tokens, return a trimmed list of messages but always keep the first message.

    Args:
        state: The current agent state containing messages
        model: The language model to use for token counting
        max_tokens: Maximum number of tokens to allow (default: DEFAULT_TOKEN_LIMIT)

    Returns:
        list[BaseMessage]: Trimmed list of messages that fits within token limit
    """
    messages = state["messages"]

    if not messages:
        return []

    first_message = messages[0]
    remaining_messages = messages[1:]

    wrapped_token_counter = create_token_counter_wrapper(model.model)

    print(f"max_input_tokens={max_input_tokens}")
    max_input_tokens = 17000
    first_tokens = wrapped_token_counter([first_message])
    print(f"first_tokens={first_tokens}")
    new_max_tokens = max_input_tokens - first_tokens

    # Calculate total tokens before trimming
    total_tokens_before = wrapped_token_counter(messages)
    print(
        f"Current token total: {total_tokens_before} (should be at least {first_tokens})"
    )

    # Verify the token count is correct
    if total_tokens_before < first_tokens:
        print(f"WARNING: Token count inconsistency detected! Recounting...")
        # Count message by message to debug
        for i, msg in enumerate(messages):
            msg_tokens = wrapped_token_counter([msg])
            print(f"  Message {i}: {msg_tokens} tokens")
        # Try alternative counting method
        alt_count = sum(wrapped_token_counter([msg]) for msg in messages)
        print(f"  Alternative count method: {alt_count} tokens")

    print_messages_compact(messages)

    trimmed_remaining = trim_messages(
        remaining_messages,
        token_counter=wrapped_token_counter,
        max_tokens=new_max_tokens,
        strategy="last",
        allow_partial=False,
    )

    result = [first_message] + trimmed_remaining

    # Only show message if some messages were trimmed
    if len(result) < len(messages):
        print(f"TRIMMED: {len(messages)} messages → {len(result)} messages")
        # Calculate total tokens after trimming
        total_tokens_after = wrapped_token_counter(result)
        print(f"New token total: {total_tokens_after}")

    return result


def sonnet_35_state_modifier(
    state: AgentState, max_input_tokens: int = DEFAULT_TOKEN_LIMIT
) -> list[BaseMessage]:
    """Given the agent state and max_tokens, return a trimmed list of messages.

    Args:
        state: The current agent state containing messages
        max_tokens: Maximum number of tokens to allow (default: DEFAULT_TOKEN_LIMIT)

    Returns:
        list[BaseMessage]: Trimmed list of messages that fits within token limit
    """
    messages = state["messages"]

    if not messages:
        return []

    first_message = messages[0]
    remaining_messages = messages[1:]
    first_tokens = estimate_messages_tokens([first_message])
    new_max_tokens = max_input_tokens - first_tokens

    # Calculate total tokens before trimming
    total_tokens_before = estimate_messages_tokens(messages)
    print(f"Current token total: {total_tokens_before}")

    trimmed_remaining = trim_messages(
        remaining_messages,
        token_counter=estimate_messages_tokens,
        max_tokens=new_max_tokens,
        strategy="last",
        allow_partial=False,
    )

    result = [first_message] + trimmed_remaining

    # Only show message if some messages were trimmed
    if len(result) < len(messages):
        print(f"TRIMMED: {len(messages)} messages → {len(result)} messages")
        # Calculate total tokens after trimming
        total_tokens_after = estimate_messages_tokens(result)
        print(f"New token total: {total_tokens_after}")

    return result


def get_model_token_limit(
    config: Dict[str, Any], agent_type: str = "default"
) -> Optional[int]:
    """Get the token limit for the current model configuration based on agent type.

    Args:
        config: Configuration dictionary containing provider and model information
        agent_type: Type of agent ("default", "research", or "planner")

    Returns:
        Optional[int]: The token limit if found, None otherwise
    """
    try:
        # Try to get config from repository for production use
        try:
            config_from_repo = get_config_repository().get_all()
            # If we succeeded, use the repository config instead of passed config
            config = config_from_repo
        except RuntimeError:
            # In tests, this may fail because the repository isn't set up
            # So we'll use the passed config directly
            pass
        if agent_type == "research":
            provider = config.get("research_provider", "") or config.get("provider", "")
            model_name = config.get("research_model", "") or config.get("model", "")
        elif agent_type == "planner":
            provider = config.get("planner_provider", "") or config.get("provider", "")
            model_name = config.get("planner_model", "") or config.get("model", "")
        else:
            provider = config.get("provider", "")
            model_name = config.get("model", "")

        try:
            from litellm import get_model_info

            provider_model = model_name if not provider else f"{provider}/{model_name}"
            model_info = get_model_info(provider_model)
            max_input_tokens = model_info.get("max_input_tokens")
            if max_input_tokens:
                logger.debug(
                    f"Using litellm token limit for {model_name}: {max_input_tokens}"
                )
                return max_input_tokens
        except Exception as e:
            logger.debug(
                f"Error getting model info from litellm: {e}, falling back to models_params"
            )

        # Fallback to models_params dict
        # Normalize model name for fallback lookup (e.g. claude-2 -> claude2)
        normalized_name = model_name.replace("-", "")
        provider_tokens = models_params.get(provider, {})
        if normalized_name in provider_tokens:
            max_input_tokens = provider_tokens[normalized_name]["token_limit"]
            logger.debug(
                f"Found token limit for {provider}/{model_name}: {max_input_tokens}"
            )
        else:
            max_input_tokens = None
            logger.debug(f"Could not find token limit for {provider}/{model_name}")

        return max_input_tokens

    except Exception as e:
        logger.warning(f"Failed to get model token limit: {e}")
        return None
