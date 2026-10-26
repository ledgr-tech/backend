"""Configuração da aplicação, lida de variáveis de ambiente.

Ver .env.example para a lista completa de variáveis esperadas.
"""

from typing import Literal

from pydantic import AliasChoices, Field, PositiveInt, field_validator
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
    #
    # Desde a issue #140, o limite diário é por CONTA (todas as empresas
    # clientes dela somadas): LLM_LIMITE_DIARIO_CONTA. LLM_LIMITE_DIARIO_EMPRESA
    # continua aceito como nome antigo da mesma variável, para a configuração
    # antiga no Railway seguir valendo; com as duas, vale a nova. O clique e a
    # geração automática gastam os mesmos limites.
    llm_limite_diario_conta: int = Field(
        default=300,
        validation_alias=AliasChoices("llm_limite_diario_conta", "llm_limite_diario_empresa"),
    )
    llm_limite_diario_global: int = 1500
    # Geração automática ao conciliar (issue #140, adendo do ADR-011): só gera
    # com LLM_HABILITADO também ligado. Desligada em produção até os textos
    # novos de Privacidade e Termos (front #190 e #197), que hoje prometem que
    # nada vai para a OpenAI sem a pessoa clicar.
    explicacao_automatica_habilitada: bool = False
    # Quantas explicações novas uma conciliação gera, as de maior valor
    # primeiro (cache hit não conta), e quantas chamadas ao provedor rodam ao
    # mesmo tempo. Com concorrência, o limite diário pode passar por no máximo
    # `llm_concorrencia_automatica` explicações.
    llm_teto_por_conciliacao: PositiveInt = 100
    llm_concorrencia_automatica: PositiveInt = 4

    # Assistente da Central de ajuda (issue #137, ADR-020): só responde com
    # esta flag e com LLM_HABILITADO ligadas. O conteúdo é o JSON que o front
    # publica no build (`ajuda_conteudo_url`; em produção,
    # https://ledgrfinance.com.br/ajuda/conteudo.json), guardado em memória
    # por 10 minutos. A Central inteira vai no contexto enquanto o texto
    # serializado couber em `assistente_maximo_conteudo_caracteres`. Os limites
    # diários são separados dos das explicações. Custo: uns US$ 0,001 por
    # pergunta (a Central inteira na entrada), uns US$ 7 por mês no pior caso
    # do limite global.
    assistente_habilitado: bool = False
    ajuda_conteudo_url: str = ""
    assistente_maximo_conteudo_caracteres: PositiveInt = 60000
    assistente_limite_diario_conta: PositiveInt = 100
    assistente_limite_diario_global: PositiveInt = 300
    assistente_max_tokens_saida: PositiveInt = 500
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

    # Login pelo Google (issue #73): o client id do client OAuth da Ledgr no
    # Google Cloud, o mesmo que o NextAuth usa na Vercel. O backend confere
    # que o `aud` do ID token é este valor (app/core/google.py). Vazio deixa
    # as rotas `*/google` em 503, sem chamar o Google, e não bloqueia o boot.
    google_client_id: str = ""

    # Consulta do CNPJ na Receita no cadastro (issue #119): se o CNPJ existe e
    # está ativo, pela BrasilAPI (app/services/consulta_cnpj/). Desligada por
    # padrão, como o e-mail e a IA: sem ela, nada é consultado e o cadastro
    # segue como antes, com a empresa não verificada. `cnpj_consulta_url` é a
    # base da API (o CNPJ entra no fim do caminho); `cnpj_consulta_chave` fica
    # vazia, porque a BrasilAPI não usa chave. O timeout total é fixo, 5 s.
    cnpj_consulta_habilitada: bool = False
    cnpj_consulta_url: str = "https://brasilapi.com.br/api/cnpj/v1"
    cnpj_consulta_chave: str = ""

    # Plano e teste (issue #122): com esta variável ligada, o POST /conciliacoes
    # responde 402 na primeira conciliação de um extrato do banco quando a
    # empresa está sem plano ou com o teste encerrado (app/services/plano.py).
    # Falsa por padrão, e só liga junto com o gateway de pagamento: antes dele,
    # quem termina o teste não tem como pagar.
    plano_bloqueio_habilitado: bool = False

    # Aprovação de justificativas (issue #132): ligada, toda justificativa nasce
    # aguardando aprovação de admin ou gestor, e o mês não fecha com pendente.
    # Falsa por padrão até o front aprovar: desligada, o backend grava a
    # aprovação junto com a justificativa, em nome de quem justificou, e nada
    # fica aguardando (app/api/conciliacoes.py). Ligar não muda o que já foi
    # gravado: o que foi aprovado com ela desligada continua aprovado.
    aprovacao_justificativas_habilitada: bool = False

    # Limites por plano (issue #133, ADR-019): quantas empresas clientes e
    # quantos usuários (ativos mais convites pendentes) a conta pode ter.
    # Inteiros positivos e opcionais: sem valor, não há limite. Os valores
    # ainda não foram decididos, e até o gateway ninguém os configura. A regra
    # mora em app/core/plano.py.
    limite_empresas_essencial: PositiveInt | None = None
    limite_empresas_padrao: PositiveInt | None = None
    limite_empresas_avancado: PositiveInt | None = None
    limite_usuarios_essencial: PositiveInt | None = None
    limite_usuarios_padrao: PositiveInt | None = None
    limite_usuarios_avancado: PositiveInt | None = None

    # Logs (issue #102): nível do logger `app`, configurado em app/core/logging.py.
    # Aceita minúsculas, e vazio vale INFO. Um valor fora da lista derruba o
    # boot com o motivo, em vez de deixar a API no ar sem log.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # /docs, /redoc e /openapi.json (issue #102). Ligado por padrão, para o
    # desenvolvimento local; em produção fica false, porque o schema lista todas
    # as rotas e parâmetros da API e não serve a ninguém fora do time.
    api_docs_habilitado: bool = True

    # Conexão MCP (issue #136, ADR-022): o Ledgr como servidor de autorização
    # OAuth do ChatGPT e do Claude do cliente. Desligada, /.well-known/oauth-*,
    # /oauth/*, /conexoes-mcp/* e /mcp respondem 404. Ligada sem
    # `mcp_url_publica` ou `oauth_issuer`, respondem 503 (o boot não cai).
    # `mcp_url_publica` é a URL canônica do servidor MCP, sem barra no fim (o
    # `resource` dos tokens: trocá-la derruba as conexões); `oauth_issuer`, a
    # base do servidor de autorização. `oauth_redirects_permitidos` são os
    # redirect_uris que o registro aceita, separados por vírgula; vazio vale
    # `REDIRECTS_PERMITIDOS_PADRAO` (app/services/oauth/redirects.py).
    mcp_habilitado: bool = False
    mcp_url_publica: str = ""
    oauth_issuer: str = ""
    oauth_redirects_permitidos: str = ""

    # Arquivo original do extrato (issue #139, ADR-023): ligada, as contas do
    # plano avançado guardam o arquivo de cada extrato enviado, cifrado pelo
    # backend, no bucket próprio do R2, e o admin e o gestor baixam o original.
    # Desligada em produção até o retorno da consultoria jurídica e o texto da
    # Política de privacidade, que hoje diz que o arquivo não é guardado. Ligada
    # sem as cinco variáveis abaixo (ou com a chave inválida), nada é guardado e
    # o download responde 503; o boot não cai. `arquivo_original_chave` é a
    # chave do AES-256-GCM, 32 bytes em base64: perdê-la é perder os arquivos.
    arquivo_original_habilitado: bool = False
    arquivo_original_r2_endpoint: str = ""
    arquivo_original_r2_bucket: str = ""
    arquivo_original_r2_access_key_id: str = ""
    arquivo_original_r2_secret_access_key: str = ""
    arquivo_original_chave: str = ""

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalizar_log_level(cls, valor: object) -> object:
        return (valor.strip().upper() or "INFO") if isinstance(valor, str) else valor

    @field_validator(
        "limite_empresas_essencial",
        "limite_empresas_padrao",
        "limite_empresas_avancado",
        "limite_usuarios_essencial",
        "limite_usuarios_padrao",
        "limite_usuarios_avancado",
        mode="before",
    )
    @classmethod
    def _limite_vazio_e_sem_limite(cls, valor: object) -> object:
        # `LIMITE_EMPRESAS_PADRAO=` no .env é o mesmo que não ter a variável.
        return None if isinstance(valor, str) and not valor.strip() else valor

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
