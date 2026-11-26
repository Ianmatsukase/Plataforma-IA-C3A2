"""Serviço de inferência: valida a solicitação e publica o trabalho na fila Redis."""
import json
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import redis
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
TTL = int(os.getenv("JOB_TTL_SEGUNDOS", "3600"))
QUEUE = "fila:inferencia"
r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
provider = TracerProvider(resource=Resource.create({"service.name": "inferencia"}))
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if endpoint:
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("c3a2.inferencia")
app = FastAPI(title="Serviço de Inferência", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)


class JobInput(BaseModel):
    texto: str = Field(min_length=1, max_length=4000)
    usuario: str = Field(min_length=1, max_length=80)
    job_id: str


@app.get("/saude")
def saude():
    return {"status": "ok", "servico": "inferencia"}


@app.post("/jobs", status_code=202)
def criar_job(dados: JobInput):
    with tracer.start_as_current_span("inference.enqueue") as span:
        span.set_attribute("messaging.system", "redis")
        span.set_attribute("messaging.destination", QUEUE)
        span.set_attribute("job.id", dados.job_id)
        carrier = {}
        propagate.inject(carrier)  # propaga traceparent para o worker
        mensagem = {"job_id": dados.job_id, "texto": dados.texto, "usuario": dados.usuario, "trace_context": carrier, "criado_em": int(time.time())}
        try:
            chave_idempotencia = f"job:enqueued:{dados.job_id}"
            novo = r.set(chave_idempotencia, "1", nx=True, ex=TTL)
            if novo:
                r.hset(f"job:{dados.job_id}", mapping={"status": "queued", "usuario": dados.usuario, "criado_em": mensagem["criado_em"]})
                r.expire(f"job:{dados.job_id}", TTL)
                r.rpush(QUEUE, json.dumps(mensagem, ensure_ascii=False))
            else:
                estado = r.hgetall(f"job:{dados.job_id}")
                if estado and estado.get("usuario") != dados.usuario:
                    raise HTTPException(status_code=409, detail="Identificador de tarefa já utilizado")
            estado_atual = r.hget(f"job:{dados.job_id}", "status") or "queued"
        except redis.RedisError:
            raise HTTPException(status_code=503, detail="Fila de processamento indisponível")
        return {"job_id": dados.job_id, "status": estado_atual, "mensagem": "Solicitação recebida para processamento assíncrono"}


@app.get("/jobs/{job_id}")
def consultar_job(job_id: str, usuario: str):
    try:
        dados = r.hgetall(f"job:{job_id}")
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Armazenamento de tarefas indisponível")
    if not dados or dados.get("usuario") != usuario:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada ou expirada")
    resposta = {"job_id": job_id, "status": dados.get("status", "unknown")}
    if dados.get("resultado"):
        resposta["resultado"] = json.loads(dados["resultado"])
    if dados.get("erro"):
        resposta["erro"] = dados["erro"]
    return resposta
