"""Configuração da aplicação, lida de variáveis de ambiente.

Ver .env.example para a lista completa de variáveis esperadas.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    nextauth_secret: str = ""  # vazio não bloqueia o boot; ver exigir_nextauth_secret()

    def exigir_nextauth_secret(self) -> str:
        """Devolve o secret de assinatura do JWT ou falha explicitamente.

        Chamado pela dependency de auth a cada request: um secret vazio
        validaria (ou assinaria) tokens com chave conhecida, então nunca pode
        cair em silêncio.
        """
        if not self.nextauth_secret.strip():
            raise RuntimeError("NEXTAUTH_SECRET não configurado: autenticação indisponível.")
        return self.nextauth_secret


settings = Settings()  # type: ignore[call-arg]
