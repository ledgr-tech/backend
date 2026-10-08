"""Configuração da aplicação, lida de variáveis de ambiente.

Ver .env.example para a lista completa de variáveis esperadas.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    nextauth_secret: str = ""  # vazio não bloqueia o boot; ver exigir_nextauth_secret()

    # IA (issue #28, ADR-011): explicação de divergências via LLM. Desligado
    # por padrão — LLM_HABILITADO nasce falso, ligar em produção depende de
    # passos fora do código (DPA, Zero Data Retention, teto de gasto no
    # painel da OpenAI). A ausência de qualquer uma destas variáveis não
    # bloqueia o boot nem os testes.
    llm_habilitado: bool = False
    llm_provedor: str = "openai"
    openai_api_key: str = ""
    openai_modelo: str = "gpt-6-luna"
    openai_base_url: str = "https://api.openai.com/v1"
    llm_timeout_segundos: float = 20.0
    llm_max_tokens_saida: int = 300
    llm_limite_diario_empresa: int = 50
    llm_limite_diario_global: int = 300

    # E-mail transacional (issue #66, ADR-012): link de recuperação de senha
    # e de aprovação da troca de e-mail, pelo Resend. Desligado por padrão,
    # como a IA — ligar em produção depende de domínio verificado (SPF,
    # DKIM, DMARC) e do DPA do provedor (#37). A ausência destas variáveis
    # não bloqueia o boot nem os testes. `frontend_url` é a base dos links:
    # vazio deixa o envio indisponível, pra nunca mandar link quebrado. Desde a
    # issue #79 quem lê esse valor passa por `app/core/frontend_url.py`, que
    # recusa URL de deploy da Vercel do mesmo jeito que recusa vazio.
    email_habilitado: bool = False
    email_provedor: str = "resend"
    resend_api_key: str = ""
    resend_base_url: str = "https://api.resend.com"
    email_remetente: str = ""
    email_responder_para: str = ""
    email_timeout_segundos: float = 10.0
    frontend_url: str = ""

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
