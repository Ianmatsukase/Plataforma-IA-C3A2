"""Gateway: autenticação JWT, rate limit, roteamento e tolerância a falhas."""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import redis
import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

JWT_SECRET = os.getenv("JWT_SEGREDO", "")
if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SEGREDO deve ter pelo menos 32 caracteres; configure o arquivo .env")
JWT_ALGORITHM = "HS256"
JWT_MINUTES = int(os.getenv("JWT_EXPIRACAO_MINUTOS", "60"))
DEMO_USER = os.getenv("USUARIO_DEMO", "aluno")
DEMO_PASSWORD = os.getenv("SENHA_DEMO", "troque-esta-senha")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_POR_MINUTO", "30"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
INFERENCE_URL = os.getenv("URL_INFERENCIA", "http://inferencia:8001")

provider = TracerProvider(resource=Resource.create({"service.name": "gateway"}))
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if endpoint:
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("c3a2.gateway")
RequestsInstrumentor().instrument()
app = FastAPI(title="Plataforma IA — API Gateway", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)
bearer = HTTPBearer(auto_error=False)


class Login(BaseModel):
    usuario: str = Field(min_length=1, max_length=80)
    senha: str = Field(min_length=1, max_length=200)


class Entrada(BaseModel):
    texto: str = Field(min_length=1, max_length=4000)


class CircuitBreaker:
    """Circuit breaker simples: fecha, abre por cooldown e testa em half-open."""
    def __init__(self, limite=3, cooldown=15):
        self.limite, self.cooldown = limite, cooldown
        self.falhas, self.aberto_ate = 0, 0.0

    def permitir(self):
        if self.aberto_ate and time.monotonic() < self.aberto_ate:
            raise HTTPException(status_code=503, detail="Circuit breaker aberto; tente novamente em instantes")
        if self.aberto_ate and time.monotonic() >= self.aberto_ate:
            self.aberto_ate = 0.0  # half-open: permite uma tentativa

    def sucesso(self):
        self.falhas, self.aberto_ate = 0, 0.0

    def falha(self):
        self.falhas += 1
        if self.falhas >= self.limite:
            self.aberto_ate = time.monotonic() + self.cooldown


breaker = CircuitBreaker()
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


def autenticar(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token JWT obrigatório", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status_code=401, detail="Token JWT inválido ou expirado", headers={"WWW-Authenticate": "Bearer"})


@app.middleware("http")
async def log_sem_dados_pessoais(request: Request, call_next):
    inicio = time.perf_counter()
    response = await call_next(request)
    # Não registrar corpo, token, texto enviado ou cabeçalhos de autenticação.
    print(f"[gateway] {request.method} {request.url.path} status={response.status_code} duracao_ms={(time.perf_counter()-inicio)*1000:.1f}")
    return response


@app.get("/saude")
def saude():
    return {"status": "ok", "servico": "gateway"}


@app.post("/login")
def login(dados: Login):
    if dados.usuario != DEMO_USER or dados.senha != DEMO_PASSWORD:
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    agora = datetime.now(timezone.utc)
    token = jwt.encode({"sub": dados.usuario, "iat": agora, "exp": agora + timedelta(minutes=JWT_MINUTES)}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "expires_in": JWT_MINUTES * 60}


def aplicar_rate_limit(cliente: str):
    janela = int(time.time() // 60)
    chave = f"rate:{cliente}:{janela}"
    try:
        pipe = redis_client.pipeline()
        pipe.incr(chave)
        pipe.expire(chave, 120)
        quantidade, _ = pipe.execute()
    except redis.RedisError:
        # Falha fechada: sem Redis, não libera tráfego ilimitado.
        raise HTTPException(status_code=503, detail="Rate limiter temporariamente indisponível")
    if quantidade > RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Limite de requisições por minuto excedido")


def chamar_inferencia(payload: dict, trace_headers: dict):
    breaker.permitir()
    ultimo_erro = None
    for tentativa in range(3):
        try:
            resposta = requests.post(f"{INFERENCE_URL}/jobs", json=payload, headers=trace_headers, timeout=(2, 5))
            resposta.raise_for_status()
            breaker.sucesso()
            return resposta.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            ultimo_erro = exc
            breaker.falha()
            if tentativa < 2:
                time.sleep(0.25 * (2 ** tentativa))  # backoff exponencial: 250 ms, 500 ms
    raise HTTPException(status_code=503, detail=f"Serviço de inferência indisponível ({type(ultimo_erro).__name__})")


@app.post("/predict", status_code=202)
def predict(entrada: Entrada, usuario: str = Depends(autenticar)):
    aplicar_rate_limit(usuario)
    with tracer.start_as_current_span("gateway.submit_job") as span:
        span.set_attribute("user.subject", usuario)
        span.set_attribute("job.text_length", len(entrada.texto))
        headers = {}
        propagate.inject(headers)
        return chamar_inferencia({"texto": entrada.texto, "usuario": usuario, "job_id": str(uuid.uuid4())}, headers)


@app.get("/jobs/{job_id}")
def consultar_job(job_id: str, usuario: str = Depends(autenticar)):
    # O gateway mantém a política de acesso; o texto submetido nunca é devolvido.
    try:
        r = requests.get(f"{INFERENCE_URL}/jobs/{job_id}", params={"usuario": usuario}, timeout=(2, 5))
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada ou expirada")
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Serviço de inferência indisponível")
