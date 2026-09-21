"""Rate limiting (slowapi) por IP, em memória — suficiente pro MVP."""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

LIMITE_UPLOAD = "10/minute"
