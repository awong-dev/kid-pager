# Bridge phone simulator: a test double, never shipped. Build context is `tools/`.

FROM python:3.12-slim

WORKDIR /app

COPY bridge_sim.py .

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

EXPOSE 8020

CMD ["python3", "bridge_sim.py", "--port", "8020"]
