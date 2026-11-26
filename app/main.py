import os, time
import redis
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI()
Instrumentator().instrument(app).expose(app)
db = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379, socket_timeout=1)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/work")
def work(n: int = 200_000):
    start = time.time()
    total = sum(i * i for i in range(n))
    try:
        served = db.incr("served")
    except redis.RedisError:
        served = -1
    return {"pod": os.getenv("HOSTNAME"),
            "ms": round((time.time() - start) * 1000, 1),
            "served": served}
