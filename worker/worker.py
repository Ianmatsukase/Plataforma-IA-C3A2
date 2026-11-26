"""Worker assíncrono: consome Redis, processa e persiste vetores no Qdrant."""
import hashlib
import json
import os
import time
import uuid

import redis
from qdrant_client import QdrantClient, models
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "solicitacoes")
QUEUE = "fila:inferencia"
TTL = int(os.getenv("JOB_TTL_SEGUNDOS", "3600"))
VECTOR_TTL = int(os.getenv("VECTOR_TTL_SEGUNDOS", "86400"))

provider = TracerProvider(resource=Resource.create({"service.name": "worker"}))
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if endpoint:
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("c3a2.worker")


def vetor_deterministico(texto: str, dimensoes: int = 64):
    """Embedding leve e determinístico para demonstrar persistência vetorial offline."""
    vetor = [0.0] * dimensoes
    tokens = texto.lower().split()
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        indice = int.from_bytes(digest[:4], "big") % dimensoes
        vetor[indice] += 1.0
    norma = sum(x * x for x in vetor) ** 0.5 or 1.0
    return [x / norma for x in vetor]


def classificar(texto: str):
    """Carga de trabalho demonstrativa; a nota avalia engenharia distribuída, não ML."""
    positivos = {"bom", "ótimo", "otimo", "excelente", "feliz", "gostei", "recomendo", "rápido", "rapido", "eficiente"}
    negativos = {"ruim", "péssimo", "pessimo", "horrível", "horrivel", "problema", "lento", "demora", "insatisfeito", "não gostei"}
    palavras = set(texto.lower().split())
    score = len(palavras & positivos) - len(palavras & negativos)
    sentimento = "positivo" if score > 0 else "negativo" if score < 0 else "neutro"
    return {"sentimento": sentimento, "pontuacao": score, "caracteres_analisados": len(texto), "motor": "classificador-demonstrativo"}


def conectar():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=5)
    q = QdrantClient(url=QDRANT_URL, timeout=5)
    while True:
        try:
            r.ping()
            q.get_collections()
            break
        except Exception as exc:
            print(f"[worker] aguardando dependencias: {type(exc).__name__}")
            time.sleep(2)
    existentes = [c.name for c in q.get_collections().collections]
    if COLLECTION not in existentes:
        q.create_collection(collection_name=COLLECTION, vectors_config=models.VectorParams(size=64, distance=models.Distance.COSINE))
    return r, q


def processar(r, q, msg):
    job_id, texto = msg["job_id"], msg["texto"]
    r.hset(f"job:{job_id}", mapping={"status": "processing"})
    resultado = classificar(texto)
    ponto_id = str(uuid.uuid5(uuid.NAMESPACE_URL, job_id))
    q.upsert(collection_name=COLLECTION, points=[models.PointStruct(id=ponto_id, vector=vetor_deterministico(texto), payload={"job_id": job_id, "sentimento": resultado["sentimento"], "criado_em": msg.get("criado_em", int(time.time()))})])
    r.hset(f"job:{job_id}", mapping={"status": "completed", "resultado": json.dumps(resultado, ensure_ascii=False), "concluido_em": int(time.time())})
    r.expire(f"job:{job_id}", TTL)
    return resultado


def limpar_vetores_expirados(q):
    """Remove vetores antigos para aplicar a política de retenção configurada."""
    limite = int(time.time()) - VECTOR_TTL
    try:
        q.delete(collection_name=COLLECTION, points_selector=models.FilterSelector(
            filter=models.Filter(must=[models.FieldCondition(key="criado_em", range=models.Range(lt=limite))])
        ))
    except Exception as exc:
        print(f"[worker] limpeza de vetores falhou: {type(exc).__name__}")


def main():
    r, q = conectar()
    print("[worker] pronto; aguardando mensagens")
    ultima_limpeza = 0
    while True:
        if time.time() - ultima_limpeza >= 60:
            limpar_vetores_expirados(q)
            ultima_limpeza = time.time()
        item = r.blpop(QUEUE, timeout=5)
        if not item:
            continue
        _, bruto = item
        msg = json.loads(bruto)
        parent = propagate.extract(msg.get("trace_context", {}))
        with tracer.start_as_current_span("worker.process_job", context=parent) as span:
            span.set_attribute("messaging.system", "redis")
            span.set_attribute("messaging.destination", QUEUE)
            span.set_attribute("job.id", msg["job_id"])
            try:
                resultado = processar(r, q, msg)
                span.set_attribute("job.status", "completed")
                print(f"[worker] job={msg['job_id']} status=completed sentimento={resultado['sentimento']}")
            except Exception as exc:
                r.hset(f"job:{msg['job_id']}", mapping={"status": "failed", "erro": "Falha interna no processamento"})
                r.expire(f"job:{msg['job_id']}", TTL)
                span.record_exception(exc)
                span.set_attribute("job.status", "failed")
                print(f"[worker] job={msg['job_id']} status=failed erro={type(exc).__name__}")


if __name__ == "__main__":
    main()
