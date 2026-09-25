"""Hash de senha com bcrypt, direto (passlib 1.7 quebra com bcrypt >= 4.1)."""

import bcrypt

# bcrypt ignora (e o pacote >= 5 rejeita) o que passa de 72 bytes.
TAMANHO_MAXIMO_BYTES = 72

# Hash descartável: comparar contra ele quando o e-mail não existe deixa o
# login com o mesmo custo de tempo, sem revelar pelo relógio quem tem conta.
_HASH_FICTICIO = bcrypt.hashpw(b"senha-ficticia", bcrypt.gensalt()).decode()


def gerar_hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def verificar(senha: str, senha_hash: str | None) -> bool:
    if len(senha.encode()) > TAMANHO_MAXIMO_BYTES:
        return False
    if senha_hash is None:
        bcrypt.checkpw(senha.encode(), _HASH_FICTICIO.encode())
        return False
    return bcrypt.checkpw(senha.encode(), senha_hash.encode())
