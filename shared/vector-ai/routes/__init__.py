"""Register all FastAPI routers on the composition-root app."""
from fastapi import FastAPI

from routes import (
    ambient,
    behaviors_http,
    chat,
    face,
    greeting,
    health,
    memory_routes,
    mood,
    sensor,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(mood.router)
    app.include_router(memory_routes.router)
    app.include_router(face.router)
    app.include_router(sensor.router)
    app.include_router(ambient.router)
    app.include_router(behaviors_http.router)
    app.include_router(greeting.router)
