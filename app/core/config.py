"""Configuração da aplicação, lida de variáveis de ambiente.

Ver .env.example para a lista completa de variáveis esperadas.
"""

from typing import Literal

from pydantic import field_validator
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
    # Limites diários (issue #87). Pior caso por explicação: 1.000 tokens de
    # entrada e 300 de saída no Luna (US$ 0,10 e US$ 0,50 por milhão), uns
    # US$ 0,00025. 1.500 por dia durante 30 dias dá uns US$ 11, abaixo do teto
    # de US$ 15/mês do projeto na OpenAI: o limite do backend bate antes.
    llm_limite_diario_empresa: int = 300
    llm_limite_diario_global: int = 1500
    # O que a descrição do lançamento leva ao provedor (issue #87, ADR-011):
    # "termos" (padrão) deixa só termos bancários e marcadores, o resto vira
    # [TEXTO]; "mascarada" manda a descrição com a máscara de CPF, CNPJ etc.,
    # mas com nomes de pessoa. "termos" vale enquanto não houver Zero Data
    # Retention. Valor fora da lista derruba o boot, como LOG_LEVEL.
    llm_modo_descricao: Literal["termos", "mascarada"] = "termos"

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

    # Rate limit (issue #64): segredo que o servidor do Next manda junto com o
    # IP do navegador, em app/core/rate_limit.py. Vazio, o IP repassado é
    # ignorado e o limite de /login, /register e /senha/* volta a ser um só
    # para todos os usuários do front. Mesmo valor nos dois lados.
    ledgr_segredo_proxy: str = ""

    # Logs (issue #102): nível do logger `app`, configurado em app/core/logging.py.
    # Aceita minúsculas, e vazio vale INFO. Um valor fora da lista derruba o
    # boot com o motivo, em vez de deixar a API no ar sem log.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # /docs, /redoc e /openapi.json (issue #102). Ligado por padrão, para o
    # desenvolvimento local; em produção fica false, porque o schema lista todas
    # as rotas e parâmetros da API e não serve a ninguém fora do time.
    api_docs_habilitado: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalizar_log_level(cls, valor: object) -> object:
        return (valor.strip().upper() or "INFO") if isinstance(valor, str) else valor

    @field_validator("llm_modo_descricao", mode="before")
    @classmethod
    def _normalizar_llm_modo_descricao(cls, valor: object) -> object:
        return (valor.strip().lower() or "termos") if isinstance(valor, str) else valor

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
