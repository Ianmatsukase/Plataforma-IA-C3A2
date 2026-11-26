.PHONY: up down logs ps test compile
up:
	docker compose up --build -d
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
ps:
	docker compose ps
test:
	python -m unittest discover -s tests -v
compile:
	python -m compileall gateway inferencia worker tests
