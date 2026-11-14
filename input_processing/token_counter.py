"""Explicit, offline counting: no implicit tokenizer downloads or model assumptions."""
from input_processing.schemas.processed_contract import JobError


def utf8_estimate(text: str) -> int:
    """Conservative byte-based estimate, NOT the selected OpenAI model's tokenizer."""
    return len(text.encode("utf-8"))


def default_token_counter():
    return utf8_estimate


def checked_counter(counter=None):
    count = counter if counter is not None else default_token_counter()

    def checked(text):
        value = count(text)
        if type(value) is not int or value < 0 or (text.strip() and value == 0):
            raise JobError("Token counter must return a positive integer for non-empty text")
        return value

    return checked
