# Publicação com HTTPS

O Compose inclui Caddy no perfil `publicacao`. O Caddy termina TLS e encaminha as chamadas para o gateway.

## Requisitos

- VM Linux com Docker Engine e Docker Compose v2;
- IP público fixo ou estável;
- domínio DNS apontando para o IP da VM;
- portas TCP 80 e 443 liberadas no firewall da nuvem e da VM.

## Passos

1. Clone o repositório na VM.
2. Copie `.env.example` para `.env`.
3. Gere um `JWT_SEGREDO` aleatório e defina `SENHA_DEMO` forte.
4. Configure `CADDY_DOMAIN` com o domínio público.
5. Execute `docker compose --profile publicacao up --build -d`.
6. Confira `https://SEU_DOMINIO/saude`.
7. Teste login, envio de tarefa e consulta do resultado.

O Caddy solicita e renova automaticamente certificados ACME. Não declare a publicação concluída até testar o endpoint HTTPS de fora da VM. Redis, Qdrant, Jaeger e inferência não devem ter portas públicas liberadas.
