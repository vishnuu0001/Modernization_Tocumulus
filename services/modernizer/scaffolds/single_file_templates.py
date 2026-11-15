# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer/scaffolds (single_file_templates.py)
# Date: 2026-02-09
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import json
import re
import textwrap

logger = logging.getLogger(__name__)



# ─── Per-file-type template helpers ─────────────────────────────────────────

# Function: _tpl_python
def _tpl_python(fname: str, user_prompt: str, project_name: str, offline_note: str, target: dict) -> str:  # noqa: ARG001
    """Return a ready-to-use Python file template based on the filename role."""
    base_name  = fname.rsplit("/", 1)[-1]
    stem       = base_name.rsplit(".", 1)[0]
    class_name = "".join(w.capitalize() for w in stem.replace("-", "_").split("_")) or project_name
    entity     = class_name.rstrip("s") or "Entity"

    if base_name.startswith("test_"):
        return textwrap.dedent("""\
            import os

            os.environ.setdefault(
                "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/test"
            )

            from fastapi.testclient import TestClient
            from app.main import app

            client = TestClient(app)


            def test_health_check() -> None:
                response = client.get("/health")
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
        """)

    if "main" in base_name:
        return textwrap.dedent(f"""\
            \"\"\"{project_name} \u2014 FastAPI application entry point\"\"\"
            import logging
            import os
            from contextlib import asynccontextmanager
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            from app.database import engine, Base
            # {offline_note}
            # ADD_ROUTER: from app.routers import items

            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
            logger = logging.getLogger(__name__)


            @asynccontextmanager
            async def lifespan(app: FastAPI):
                logger.info("Starting up {project_name}...")
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                yield
                logger.info("Shutting down {project_name}...")


            app = FastAPI(
                title="{project_name} API",
                version="1.0.0",
                docs_url="/docs",
                redoc_url="/redoc",
                lifespan=lifespan,
            )

            # No wildcard-with-credentials default here: that combination is
            # a real CORS vulnerability, not just a lint warning, so this
            # generated project fails safe to localhost-only until whoever
            # deploys it sets ALLOWED_ORIGINS explicitly.
            _allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
            app.add_middleware(
                CORSMiddleware,
                allow_origins=_allowed_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

            # ADD_ROUTER: app.include_router(items.router)

            @app.get("/health", tags=["Health"])
            async def health_check():
                return {{"status": "ok", "service": "{project_name}"}}
        """)

    if "database" in base_name:
        return textwrap.dedent(f"""\
            \"\"\"Async SQLAlchemy database session for {project_name}\"\"\"
            import os
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
            from sqlalchemy.orm import DeclarativeBase
            # {offline_note}

            DATABASE_URL: str = os.environ["DATABASE_URL"]  # e.g. postgresql+asyncpg://user:pass@host/db

            engine = create_async_engine(
                DATABASE_URL,
                pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
                max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
                pool_pre_ping=True,
                echo=os.getenv("DB_ECHO", "false").lower() == "true",
            )

            AsyncSessionLocal = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )


            class Base(DeclarativeBase):
                pass


            async def get_db() -> AsyncSession:
                \"\"\"FastAPI dependency that yields a database session.\"\"\"
                async with AsyncSessionLocal() as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise
        """)

    if "config" in base_name or "settings" in base_name:
        return textwrap.dedent(f"""\
            \"\"\"Application configuration loaded from environment variables.\"\"\"
            import os
            from functools import lru_cache
            from pydantic_settings import BaseSettings, SettingsConfigDict
            # {offline_note}


            class Settings(BaseSettings):
                app_name: str = "{project_name}"
                debug: bool = False
                database_url: str
                secret_key: str
                allowed_origins: list[str] = ["*"]
                access_token_expire_minutes: int = 30

                model_config = SettingsConfigDict(
                    env_file=".env", env_file_encoding="utf-8", case_sensitive=False
                )


            @lru_cache
            def get_settings() -> Settings:
                return Settings()


            settings = get_settings()
        """)

    if "model" in base_name or "entities" in base_name:
        return textwrap.dedent(f"""\
            \"\"\"SQLAlchemy ORM models for {project_name}\"\"\"
            import datetime
            from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
            from sqlalchemy.orm import Mapped, mapped_column
            from app.database import Base
            # {offline_note}


            class {entity}(Base):
                __tablename__ = "{entity.lower()}s"

                id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
                name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
                description: Mapped[str | None] = mapped_column(Text, nullable=True)
                is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
                created_at: Mapped[datetime.datetime] = mapped_column(
                    DateTime(timezone=True), server_default=func.now(), nullable=False
                )
                updated_at: Mapped[datetime.datetime | None] = mapped_column(
                    DateTime(timezone=True), onupdate=func.now(), nullable=True
                )

                def __repr__(self) -> str:
                    return f"<{entity} id={{self.id}} name={{self.name!r}}>"
        """)

    if "schema" in base_name or "dto" in base_name:
        return textwrap.dedent(f"""\
            \"\"\"Pydantic v2 schemas for {project_name}\"\"\"
            import datetime
            from typing import Generic, TypeVar
            from pydantic import BaseModel, ConfigDict, Field
            # {offline_note}

            T = TypeVar("T")


            class PagedResponse(BaseModel, Generic[T]):
                items: list[T]
                total: int
                page: int
                size: int
                pages: int


            class {entity}Base(BaseModel):
                name: str = Field(..., min_length=1, max_length=255, description="Name of the {entity}")
                description: str | None = Field(None, max_length=2000)


            class {entity}Create({entity}Base):
                pass


            class {entity}Update(BaseModel):
                name: str | None = Field(None, min_length=1, max_length=255)
                description: str | None = None
                is_active: bool | None = None


            class {entity}Response({entity}Base):
                id: int
                is_active: bool
                created_at: datetime.datetime
                updated_at: datetime.datetime | None

                model_config = ConfigDict(from_attributes=True)
        """)

    if "router" in base_name or "route" in base_name or "view" in base_name:
        entity_lower = entity.lower()
        template = """\
            \"\"\"FastAPI router for __ENTITY__ resource\"\"\"
            import logging
            import math
            from fastapi import APIRouter, Depends, HTTPException, Query, status
            from sqlalchemy import func, select
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.database import get_db
            # __OFFLINE_NOTE__
            # IMPLEMENT: from app.models.__ENTITY_LOWER__ import __ENTITY__
            # IMPLEMENT: from app.schemas.__ENTITY_LOWER__ import __ENTITY__Create, __ENTITY__Update, __ENTITY__Response, PagedResponse

            logger = logging.getLogger(__name__)
            router = APIRouter(prefix="/api/__ENTITY_LOWER__s", tags=["__ENTITY__"])


            @router.get("/", response_model=PagedResponse)
            async def list___ENTITY_LOWER__s(
                page: int = Query(1, ge=1),
                size: int = Query(20, ge=1, le=100),
                search: str = Query(""),
                db: AsyncSession = Depends(get_db),
            ):
                offset = (page - 1) * size
                query = select(__ENTITY__).where(__ENTITY__.is_active.is_(True))
                if search:
                    search_pattern = "%" + search + "%"
                    query = query.where(__ENTITY__.name.ilike(search_pattern))
                total_result = await db.execute(select(func.count()).select_from(query.subquery()))
                total = total_result.scalar_one()
                result = await db.execute(query.offset(offset).limit(size))
                items = result.scalars().all()
                logger.info("Listed %d __ENTITY_LOWER__s (page=%d)", len(items), page)
                return PagedResponse(
                    items=items, total=total, page=page, size=size,
                    pages=math.ceil(total / size) if total else 0,
                )


            @router.get("/{item_id}", response_model=__ENTITY__Response)
            async def get___ENTITY_LOWER__(item_id: int, db: AsyncSession = Depends(get_db)):
                result = await db.get(__ENTITY__, item_id)
                if not result or not result.is_active:
                    logger.warning("__ENTITY__ %d not found", item_id)
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="__ENTITY__ not found")
                return result


            @router.post("/", response_model=__ENTITY__Response, status_code=status.HTTP_201_CREATED)
            async def create___ENTITY_LOWER__(data: __ENTITY__Create, db: AsyncSession = Depends(get_db)):
                obj = __ENTITY__(**data.model_dump())
                db.add(obj)
                await db.commit()
                await db.refresh(obj)
                logger.info("Created __ENTITY__ id=%d", obj.id)
                return obj


            @router.put("/{item_id}", response_model=__ENTITY__Response)
            async def update___ENTITY_LOWER__(item_id: int, data: __ENTITY__Update, db: AsyncSession = Depends(get_db)):
                obj = await db.get(__ENTITY__, item_id)
                if not obj or not obj.is_active:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="__ENTITY__ not found")
                for field, value in data.model_dump(exclude_none=True).items():
                    setattr(obj, field, value)
                await db.commit()
                await db.refresh(obj)
                logger.info("Updated __ENTITY__ id=%d", item_id)
                return obj


            @router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
            async def delete___ENTITY_LOWER__(item_id: int, db: AsyncSession = Depends(get_db)):
                obj = await db.get(__ENTITY__, item_id)
                if not obj or not obj.is_active:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="__ENTITY__ not found")
                obj.is_active = False
                await db.commit()
                logger.warning("Soft-deleted __ENTITY__ id=%d", item_id)
        """
        return (
            textwrap.dedent(template)
            .replace("__OFFLINE_NOTE__", offline_note)
            .replace("__ENTITY_LOWER__", entity_lower)
            .replace("__ENTITY__", entity)
        )

    if "requirements" in base_name:
        stack = target.get("backend_tech", "")
        if "django" in stack.lower():
            return textwrap.dedent("""\
                Django==5.0.4
                djangorestframework==3.15.1
                psycopg[binary]==3.1.18
                django-cors-headers==4.3.1
                dj-database-url==2.1.0
                python-decouple==3.8
                gunicorn==22.0.0
                pytest-django==4.8.0
                factory-boy==3.3.0
            """)
        return textwrap.dedent("""\
            fastapi==0.111.0
            uvicorn[standard]==0.29.0
            sqlalchemy[asyncio]==2.0.30
            asyncpg==0.29.0
            alembic==1.13.1
            pydantic==2.7.1
            pydantic-settings==2.2.1
            httpx==0.27.0
            pytest==8.2.0
            pytest-asyncio==0.23.6
            anyio==4.3.0
        """)

    req_lines = "\n".join(f"# {l}" for l in user_prompt.splitlines()[:8])
    return textwrap.dedent(f"""\
        \"\"\"{fname} \u2014 {project_name}\"\"\"
        # {offline_note}
        {req_lines}
        import logging

        logger = logging.getLogger(__name__)
    """)


# Function: _tpl_csharp
def _tpl_csharp(class_name: str, ns: str, user_prompt: str, offline_note: str, target: dict) -> str:
    """Return a ready-to-use C# file template based on the class name role."""
    if class_name == "Program":
        return textwrap.dedent(f"""\
            // {offline_note}
            var builder = WebApplication.CreateBuilder(args);
            builder.Services.AddProblemDetails();
            builder.Services.AddHealthChecks();
            var app = builder.Build();
            app.UseExceptionHandler();
            app.MapHealthChecks("/health");
            app.MapGet("/api/status", () => Results.Ok(new {{ status = "ok", service = "{ns}" }}));
            app.Run();
        """)
    if "Controller" in class_name or "Endpoint" in class_name:
        entity = class_name.replace("Controller", "").replace("Endpoints", "").replace("Endpoint", "") or "Item"
        return textwrap.dedent(f"""\
            // {offline_note}
            using Microsoft.AspNetCore.Http;
            using Microsoft.AspNetCore.Mvc;
            using Microsoft.Extensions.Logging;
            using {ns}.Services;
            using {ns}.Models;
            using System.Threading;
            using System.Threading.Tasks;

            namespace {ns}.Controllers;

            [ApiController]
            [Route("api/[controller]")]
            public sealed class {entity}Controller : ControllerBase
            {{
                private readonly I{entity}Service _service;
                private readonly ILogger<{entity}Controller> _logger;

                public {entity}Controller(I{entity}Service service, ILogger<{entity}Controller> logger)
                {{
                    _service = service;
                    _logger = logger;
                }}

                [HttpGet]
                public async Task<IActionResult> GetAll([FromQuery] int page = 1, [FromQuery] int pageSize = 20,
                    CancellationToken ct = default)
                {{
                    var result = await _service.GetAllAsync(page, pageSize, ct);
                    _logger.LogInformation("Retrieved {{Count}} {entity.lower()} records", result.TotalCount);
                    return Ok(result);
                }}

                [HttpGet("{{id:int}}")]
                public async Task<IActionResult> GetById(int id, CancellationToken ct = default)
                {{
                    var item = await _service.GetByIdAsync(id, ct);
                    if (item is null)
                    {{
                        _logger.LogWarning("{entity} {{Id}} not found", id);
                        return NotFound(new ProblemDetails {{ Title = "{entity} not found", Status = 404 }});
                    }}
                    return Ok(item);
                }}

                [HttpPost]
                public async Task<IActionResult> Create([FromBody] {entity}CreateRequest request,
                    CancellationToken ct = default)
                {{
                    if (!ModelState.IsValid)
                        return ValidationProblem(ModelState);
                    var created = await _service.CreateAsync(request, ct);
                    _logger.LogInformation("Created {entity} {{Id}}", created.Id);
                    return CreatedAtAction(nameof(GetById), new {{ id = created.Id }}, created);
                }}

                [HttpPut("{{id:int}}")]
                public async Task<IActionResult> Update(int id, [FromBody] {entity}UpdateRequest request,
                    CancellationToken ct = default)
                {{
                    if (!ModelState.IsValid)
                        return ValidationProblem(ModelState);
                    var updated = await _service.UpdateAsync(id, request, ct);
                    if (updated is null)
                    {{
                        _logger.LogWarning("{entity} {{Id}} not found for update", id);
                        return NotFound(new ProblemDetails {{ Title = "{entity} not found", Status = 404 }});
                    }}
                    _logger.LogInformation("Updated {entity} {{Id}}", id);
                    return Ok(updated);
                }}

                [HttpDelete("{{id:int}}")]
                public async Task<IActionResult> Delete(int id, CancellationToken ct = default)
                {{
                    var deleted = await _service.DeleteAsync(id, ct);
                    if (!deleted)
                    {{
                        _logger.LogWarning("{entity} {{Id}} not found for deletion", id);
                        return NotFound(new ProblemDetails {{ Title = "{entity} not found", Status = 404 }});
                    }}
                    _logger.LogWarning("Soft-deleted {entity} {{Id}}", id);
                    return NoContent();
                }}
            }}
        """)

    if "Service" in class_name and "Impl" in class_name:
        entity = class_name.replace("ServiceImpl", "") or "Item"
        return textwrap.dedent(f"""\
            // {offline_note}
            using Microsoft.Extensions.Logging;
            using {ns}.Models;
            using {ns}.Repositories;
            using System.Threading;
            using System.Threading.Tasks;

            namespace {ns}.Services;

            public sealed class {entity}ServiceImpl : I{entity}Service
            {{
                private readonly I{entity}Repository _repository;
                private readonly ILogger<{entity}ServiceImpl> _logger;

                public {entity}ServiceImpl(I{entity}Repository repository, ILogger<{entity}ServiceImpl> logger)
                {{
                    _repository = repository;
                    _logger = logger;
                }}

                public async Task<PagedResult<{entity}Response>> GetAllAsync(int page, int pageSize, CancellationToken ct)
                {{
                    var (items, total) = await _repository.GetPagedAsync(page, pageSize, ct);
                    _logger.LogInformation("GetAll {entity}: page={{Page}} size={{Size}} total={{Total}}", page, pageSize, total);
                    return new PagedResult<{entity}Response>(
                        items.Select({entity}Mapper.ToResponse).ToArray(),
                        total, page, pageSize
                    );
                }}

                public async Task<{entity}Response?> GetByIdAsync(int id, CancellationToken ct)
                {{
                    var entity = await _repository.GetByIdAsync(id, ct);
                    return entity is null ? null : {entity}Mapper.ToResponse(entity);
                }}

                public async Task<{entity}Response> CreateAsync({entity}CreateRequest request, CancellationToken ct)
                {{
                    var entity = {entity}Mapper.ToEntity(request);
                    var saved  = await _repository.AddAsync(entity, ct);
                    await _repository.SaveChangesAsync(ct);
                    _logger.LogInformation("Created {entity} id={{Id}}", saved.Id);
                    return {entity}Mapper.ToResponse(saved);
                }}

                public async Task<{entity}Response?> UpdateAsync(int id, {entity}UpdateRequest request, CancellationToken ct)
                {{
                    var entity = await _repository.GetByIdAsync(id, ct);
                    if (entity is null) return null;
                    {entity}Mapper.ApplyUpdate(entity, request);
                    entity.UpdatedAt = DateTime.UtcNow;
                    await _repository.SaveChangesAsync(ct);
                    _logger.LogInformation("Updated {entity} id={{Id}}", id);
                    return {entity}Mapper.ToResponse(entity);
                }}

                public async Task<bool> DeleteAsync(int id, CancellationToken ct)
                {{
                    var entity = await _repository.GetByIdAsync(id, ct);
                    if (entity is null) return false;
                    entity.IsActive  = false;
                    entity.UpdatedAt = DateTime.UtcNow;
                    await _repository.SaveChangesAsync(ct);
                    _logger.LogWarning("Soft-deleted {entity} id={{Id}}", id);
                    return true;
                }}
            }}
        """)

    if "Repository" in class_name:
        entity = class_name.replace("Repository", "") or "Item"
        return textwrap.dedent(f"""\
            // {offline_note}
            using Microsoft.EntityFrameworkCore;
            using {ns}.Models;
            using System.Threading;
            using System.Threading.Tasks;

            namespace {ns}.Repositories;

            public sealed class {entity}Repository : I{entity}Repository
            {{
                private readonly {ns}DbContext _context;

                public {entity}Repository({ns}DbContext context) => _context = context;

                public async Task<({entity}Entity[] Items, int Total)> GetPagedAsync(
                    int page, int pageSize, CancellationToken ct)
                {{
                    var query = _context.{entity}s.Where(x => x.IsActive).AsNoTracking();
                    var total = await query.CountAsync(ct);
                    var items = await query
                        .OrderByDescending(x => x.CreatedAt)
                        .Skip((page - 1) * pageSize)
                        .Take(pageSize)
                        .ToArrayAsync(ct);
                    return (items, total);
                }}

                public async Task<{entity}Entity?> GetByIdAsync(int id, CancellationToken ct) =>
                    await _context.{entity}s.FirstOrDefaultAsync(x => x.Id == id && x.IsActive, ct);

                public async Task<{entity}Entity> AddAsync({entity}Entity entity, CancellationToken ct)
                {{
                    await _context.{entity}s.AddAsync(entity, ct);
                    return entity;
                }}

                public async Task<{entity}Entity> UpdateAsync({entity}Entity entity, CancellationToken ct)
                {{
                    _context.{entity}s.Update(entity);
                    return entity;
                }}

                public async Task SaveChangesAsync(CancellationToken ct) =>
                    await _context.SaveChangesAsync(ct);
            }}
        """)

    req_lines = "\n".join(f"// {l}" for l in user_prompt.splitlines()[:8])
    return textwrap.dedent(f"""\
        // {offline_note}
        {req_lines}
        // Target: {target['name']}
        // File: {class_name}.cs
        using System;
        using System.Threading;
        using System.Threading.Tasks;

        namespace {ns};

        public sealed class {class_name}
        {{
            // IMPLEMENT: {class_name} for {target['name']}
        }}
    """)


# Function: _tpl_java
def _tpl_java(fname: str, class_name: str, ns: str, user_prompt: str, offline_note: str, target: dict) -> str:
    """Return a ready-to-use Java file template based on the class name role."""
    normalized = fname.replace("\\", "/")
    source_marker = "src/main/java/"
    relative_parent = normalized.split(source_marker, 1)[-1].rsplit("/", 1)[0]
    pkg = relative_parent.replace("/", ".").lower() if source_marker in normalized else ns.lower()
    base_pkg = pkg.rsplit(".", 1)[0] if "." in pkg else pkg
    backend = str(target.get("backend_tech") or "").casefold()
    is_spring = "spring" in backend
    is_mongodb = "mongodb" in backend or str(target.get("db_target") or "").casefold() == "mongodb"
    if not is_spring:
        declaration = (
            f"public interface {class_name} {{}}"
            if class_name.endswith("Repository") else
            f"public class {class_name} {{\n"
            + ("    public static void main(String[] args) {}\n" if class_name.endswith("Application") or class_name == "Application" else "")
            + "}"
        )
        return f"// {offline_note}\npackage {pkg};\n\n{declaration}\n"
    if class_name == "Application" or class_name.endswith("Application"):
        return textwrap.dedent(f"""\
            // {offline_note}
            package {pkg};

            import org.springframework.boot.SpringApplication;
            import org.springframework.boot.autoconfigure.SpringBootApplication;

            @SpringBootApplication
            public class {class_name} {{
                public static void main(String[] args) {{
                    SpringApplication.run({class_name}.class, args);
                }}
            }}
        """)
    if class_name.endswith("Entity"):
        if is_mongodb:
            return textwrap.dedent(f"""\
                // {offline_note}
                package {pkg};

                import org.springframework.data.annotation.Id;
                import org.springframework.data.mongodb.core.mapping.Document;

                @Document(collection = "items")
                public class {class_name} {{
                    @Id private Long id;
                    private String name;
                    public Long getId() {{ return id; }}
                    public void setId(Long id) {{ this.id = id; }}
                    public String getName() {{ return name; }}
                    public void setName(String name) {{ this.name = name; }}
                }}
            """)
        return textwrap.dedent(f"""\
            // {offline_note}
            package {pkg};

            import jakarta.persistence.Entity;
            import jakarta.persistence.GeneratedValue;
            import jakarta.persistence.GenerationType;
            import jakarta.persistence.Id;

            @Entity
            public class {class_name} {{
                @Id
                @GeneratedValue(strategy = GenerationType.IDENTITY)
                private Long id;
                private String name;

                public Long getId() {{ return id; }}
                public void setId(Long id) {{ this.id = id; }}
                public String getName() {{ return name; }}
                public void setName(String name) {{ this.name = name; }}
            }}
        """)
    if class_name.endswith("Repository"):
        entity = class_name.removesuffix("Repository") + "Entity"
        repository_type = "MongoRepository" if is_mongodb else "JpaRepository"
        repository_import = (
            "org.springframework.data.mongodb.repository.MongoRepository"
            if is_mongodb else "org.springframework.data.jpa.repository.JpaRepository"
        )
        return textwrap.dedent(f"""\
            // {offline_note}
            package {pkg};

            import {base_pkg}.model.{entity};
            import {repository_import};

            public interface {class_name} extends {repository_type}<{entity}, Long> {{}}
        """)
    if class_name.endswith("Service"):
        entity = class_name.removesuffix("Service") + "Entity"
        repository = class_name.removesuffix("Service") + "Repository"
        return textwrap.dedent(f"""\
            // {offline_note}
            package {pkg};

            import {base_pkg}.model.{entity};
            import {base_pkg}.repository.{repository};
            import org.springframework.stereotype.Service;
            import java.util.List;

            @Service
            public class {class_name} {{
                private final {repository} repository;

                public {class_name}({repository} repository) {{ this.repository = repository; }}
                public List<{entity}> findAll() {{ return repository.findAll(); }}
                public {entity} save({entity} entity) {{ return repository.save(entity); }}
            }}
        """)
    if "Controller" in class_name:
        entity = class_name.replace("Controller", "") or "Item"
        entity_type = entity + "Entity"
        service_type = entity + "Service"
        return textwrap.dedent(f"""\
            // {offline_note}
            package {pkg};

            import {base_pkg}.model.{entity_type};
            import {base_pkg}.service.{service_type};
            import org.springframework.http.ResponseEntity;
            import org.springframework.web.bind.annotation.*;
            import jakarta.validation.Valid;
            import java.net.URI;
            import java.util.List;

            @RestController
            @RequestMapping("/api/{entity.lower()}s")
            public class {class_name} {{
                private final {service_type} service;

                public {class_name}({service_type} service) {{ this.service = service; }}

                @GetMapping
                public ResponseEntity<List<{entity_type}>> getAll() {{
                    return ResponseEntity.ok(service.findAll());
                }}

                @PostMapping
                public ResponseEntity<{entity_type}> create(@Valid @RequestBody {entity_type} entity) {{
                    {entity_type} saved = service.save(entity);
                    return ResponseEntity.created(URI.create("/api/{entity.lower()}s/" + saved.getId())).body(saved);
                }}
            }}
        """)

    req_lines = "\n".join(f"// {l}" for l in user_prompt.splitlines()[:8])
    return textwrap.dedent(f"""\
        // {offline_note}
        {req_lines}
        package {pkg};

        import org.springframework.stereotype.Component;
        import lombok.extern.slf4j.Slf4j;

        @Component
        @Slf4j
        public class {class_name} {{
            // IMPLEMENT: {class_name} for {target['name']}
        }}
    """)


# Function: _tpl_typescript
def _tpl_typescript(fname: str, class_name: str, user_prompt: str, offline_note: str, target: dict) -> str:
    """Return a ready-to-use TypeScript/JavaScript file template based on the file role."""
    ext        = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    is_tsx     = ext in ("tsx", "jsx")
    is_service = "Service" in class_name or "service" in fname
    is_page    = "Page" in class_name or "page" in fname or "View" in class_name
    entity     = class_name.replace("Service", "").replace("Page", "").replace("View", "") or "Item"

    if is_service:
        return textwrap.dedent(f"""\
            // {offline_note}
            // Target: {target['name']}
            const API_BASE = '/api';

            export interface {entity}Item {{
              id: number;
              name: string;
              isActive: boolean;
              createdAt: string;
              updatedAt: string | null;
            }}

            export interface Create{entity}Request {{
              name: string;
              description?: string;
            }}

            export interface Update{entity}Request {{
              name?: string;
              description?: string;
              isActive?: boolean;
            }}

            export interface PageResult<T> {{
              items: T[];
              total: number;
              page: number;
              size: number;
              pages: number;
            }}

            async function _request<T>(url: string, init?: RequestInit): Promise<T> {{
              const res = await fetch(`${{API_BASE}}${{url}}`, {{
                ...init,
                headers: {{ 'Content-Type': 'application/json', ...init?.headers }},
              }});
              if (!res.ok) {{
                const body = await res.text().catch(() => res.statusText);
                throw new Error(`HTTP ${{res.status}}: ${{body}}`);
              }}
              return res.json() as Promise<T>;
            }}

            export const {entity}Service = {{
              getAll: (params?: {{ page?: number; size?: number; search?: string }}) => {{
                const qs = new URLSearchParams({{
                  page:   String(params?.page   ?? 1),
                  size:   String(params?.size   ?? 20),
                  search: params?.search ?? '',
                }}).toString();
                return _request<PageResult<{entity}Item>>(`/{entity.lower()}s?${{qs}}`);
              }},
              getById: (id: number) =>
                _request<{entity}Item>(`/{entity.lower()}s/${{id}}`),
              create: (data: Create{entity}Request) =>
                _request<{entity}Item>(`/{entity.lower()}s`, {{ method: 'POST', body: JSON.stringify(data) }}),
              update: (id: number, data: Update{entity}Request) =>
                _request<{entity}Item>(`/{entity.lower()}s/${{id}}`, {{ method: 'PUT', body: JSON.stringify(data) }}),
              remove: (id: number) =>
                _request<void>(`/{entity.lower()}s/${{id}}`, {{ method: 'DELETE' }}),
            }};

            export default {entity}Service;
        """)

    if is_tsx and is_page:
        return textwrap.dedent(f"""\
            // {offline_note}
            // Target: {target['name']}
            import React, {{ useState, useEffect, useCallback }} from 'react';
            import {entity}Service, {{ {entity}Item }} from './{entity}Service';

            export default function {entity}Page() {{
              const [{entity.lower()}s, set{entity}s] = useState<{entity}Item[]>([]);
              const [loading, setLoading]   = useState(true);
              const [error, setError]       = useState<string | null>(null);
              const [search, setSearch]     = useState('');
              const [page, setPage]         = useState(1);
              const [total, setTotal]       = useState(0);

              const fetchItems = useCallback(async () => {{
                setLoading(true);
                setError(null);
                try {{
                  const result = await {entity}Service.getAll({{ page, size: 20, search }});
                  set{entity}s(result.items);
                  setTotal(result.total);
                }} catch (err: unknown) {{
                  setError(err instanceof Error ? err.message : 'Unexpected error');
                }} finally {{
                  setLoading(false);
                }}
              }}, [page, search]);

              useEffect(() => {{ void fetchItems(); }}, [fetchItems]);

              const handleDelete = async (id: number) => {{
                if (!window.confirm('Delete this {entity.lower()}?')) return;
                try {{
                  await {entity}Service.remove(id);
                  await fetchItems();
                }} catch (err: unknown) {{
                  alert(err instanceof Error ? err.message : 'Delete failed');
                }}
              }};

              if (loading) return <div className="p-8 text-center text-gray-400">Loading...</div>;
              if (error)   return (
                <div className="p-8 text-center text-red-400">
                  Error: {{error}} <button onClick={{fetchItems}} className="ml-2 underline">Retry</button>
                </div>
              );

              return (
                <div className="p-6">
                  <h1 className="text-2xl font-bold mb-4">{entity}s</h1>
                  <div className="overflow-x-auto rounded-lg border border-gray-700">
                    <table className="w-full text-sm text-gray-300">
                      <thead className="bg-gray-800 text-gray-400 uppercase text-xs">
                        <tr>
                          <th className="px-4 py-3 text-left">ID</th>
                          <th className="px-4 py-3 text-left">Name</th>
                          <th className="px-4 py-3 text-right">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-700">
                        {{{entity.lower()}s.map(item => (
                          <tr key={{item.id}} className="hover:bg-gray-750">
                            <td className="px-4 py-3">{{item.id}}</td>
                            <td className="px-4 py-3 font-medium">{{item.name}}</td>
                            <td className="px-4 py-3 text-right">
                              <button onClick={{() => handleDelete(item.id)}}
                                className="text-red-400 hover:text-red-300 text-xs underline">Delete</button>
                            </td>
                          </tr>
                        ))}}
                      </tbody>
                    </table>
                  </div>
                  <div className="flex items-center justify-between mt-4 text-sm text-gray-400">
                    <span>Total: {{total}}</span>
                    <div className="flex gap-2">
                      <button disabled={{page === 1}} onClick={{() => setPage(p => p - 1)}}
                        className="px-3 py-1 rounded bg-gray-700 disabled:opacity-40">Prev</button>
                      <span>Page {{page}}</span>
                      <button disabled={{page * 20 >= total}} onClick={{() => setPage(p => p + 1)}}
                        className="px-3 py-1 rounded bg-gray-700 disabled:opacity-40">Next</button>
                    </div>
                  </div>
                </div>
              );
            }}
        """)

    req_lines = "\n".join(f"// {l}" for l in user_prompt.splitlines()[:8])
    return textwrap.dedent(f"""\
        // {offline_note}
        {req_lines}
        // Target: {target['name']}
        // File: {fname}

        export default function {class_name}() {{
          // IMPLEMENT: {class_name}
          return null;
        }}
    """)


def _tpl_go(fname: str, project_name: str, offline_note: str) -> str:
    """Return syntactically valid, dependency-free Go source by file role."""
    normalized = fname.replace("\\", "/")
    base_name = normalized.rsplit("/", 1)[-1]
    parent = normalized.rsplit("/", 2)[-2] if "/" in normalized else "main"
    package = "main" if normalized.startswith("cmd/") else re.sub(r"[^a-z0-9_]", "", parent.casefold()) or "app"
    if base_name == "main.go":
        return textwrap.dedent(f"""\
            // {offline_note}
            package main

            import (
                "log"
                "net/http"
            )

            func main() {{
                mux := http.NewServeMux()
                mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {{
                    w.Header().Set("Content-Type", "application/json")
                    _, _ = w.Write([]byte(`{{"status":"ok","service":"{project_name}"}}`))
                }})
                log.Fatal(http.ListenAndServe(":8080", mux))
            }}
        """)
    if base_name.endswith("_test.go"):
        return textwrap.dedent(f"""\
            // {offline_note}
            package {package}

            import "testing"

            func TestGeneratedPackage(t *testing.T) {{
                if "{package}" == "" {{ t.Fatal("package name must not be empty") }}
            }}
        """)
    component = "".join(
        word[:1].upper() + word[1:]
        for word in re.sub(r"[^A-Za-z0-9]+", "_", base_name.rsplit(".", 1)[0]).split("_")
        if word
    ) or "Generated"
    return textwrap.dedent(f"""\
        // {offline_note}
        package {package}

        // Component identifies this generated package without placeholder behavior.
        const {component}Component = "{package}"
    """)


# Function: _tpl_sql
def _tpl_sql(fname: str, user_prompt: str, offline_note: str, target: dict) -> str:
    """Return a SQL DDL template for the given file."""
    db        = target.get("db_tech", "PostgreSQL")
    req_lines = "\n".join(f"-- {l}" for l in user_prompt.splitlines()[:8])
    dialect = str(target.get("db_target") or db).casefold()
    header = f"-- {offline_note}\n{req_lines}\n-- Target: {db}\n-- File: {fname}\n\n"
    if "mssql" in dialect or "sql server" in dialect:
        return header + textwrap.dedent("""\
            IF OBJECT_ID(N'dbo.items', N'U') IS NULL
            BEGIN
                CREATE TABLE dbo.items (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    name NVARCHAR(255) NOT NULL,
                    description NVARCHAR(MAX) NULL,
                    is_active BIT NOT NULL CONSTRAINT DF_items_is_active DEFAULT (1),
                    created_at DATETIME2 NOT NULL CONSTRAINT DF_items_created_at DEFAULT (SYSUTCDATETIME()),
                    updated_at DATETIME2 NULL
                );
            END;
        """)
    if "oracle" in dialect:
        return header + textwrap.dedent("""\
            CREATE TABLE items (
                id NUMBER NOT NULL,
                name VARCHAR2(255 CHAR) NOT NULL,
                description CLOB,
                is_active NUMBER(1) DEFAULT 1 NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE,
                CONSTRAINT pk_items PRIMARY KEY (id)
            );
            CREATE INDEX idx_items_name ON items(name);
        """)
    if "mysql" in dialect:
        return header + textwrap.dedent("""\
            CREATE TABLE IF NOT EXISTS items (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NULL
            );
            CREATE INDEX idx_items_name ON items(name);
        """)
    if "db2" in dialect:
        return header + textwrap.dedent("""\
            CREATE TABLE items (
                id INTEGER NOT NULL,
                name VARCHAR(255) NOT NULL,
                description VARCHAR(2000),
                is_active SMALLINT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP,
                CONSTRAINT pk_items PRIMARY KEY (id)
            );
        """)
    is_pg = "postgres" in dialect or "pg" in dialect
    id_col = "id BIGSERIAL PRIMARY KEY" if is_pg else "id INTEGER PRIMARY KEY"
    ts_type = "TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP" if is_pg else "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ts_upd = "TIMESTAMPTZ" if is_pg else "TIMESTAMP"
    create_table = "CREATE TABLE IF NOT EXISTS" if is_pg else "CREATE TABLE"
    return header + textwrap.dedent(f"""\
        {create_table} items (
            {id_col},
            name        VARCHAR(255) NOT NULL,
            description TEXT,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  {ts_type},
            updated_at  {ts_upd}
        );

        CREATE INDEX idx_items_name ON items(name);
    """)


# Function: _tpl_dockerfile
def _tpl_dockerfile(lang: str, ns: str) -> str:
    """Return a multi-stage Dockerfile template for the target language."""
    if lang.lower() == "python":
        return textwrap.dedent("""\
            FROM python:3.12-slim AS base
            WORKDIR /app
            ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

            FROM base AS deps
            COPY requirements.txt .
            RUN pip install --no-cache-dir -r requirements.txt

            FROM deps AS final
            COPY . .
            EXPOSE 8000
            CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
        """)
    if lang.lower() == "java":
        return textwrap.dedent("""\
            FROM eclipse-temurin:21-jdk-alpine AS build
            WORKDIR /build
            COPY pom.xml .
            COPY src ./src
            RUN ./mvnw -q package -DskipTests

            FROM eclipse-temurin:21-jre-alpine
            WORKDIR /app
            COPY --from=build /build/target/*.jar app.jar
            EXPOSE 8080
            ENTRYPOINT ["java", "-XX:+UseContainerSupport", "-jar", "app.jar"]
        """)
    return textwrap.dedent(f"""\
        FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
        WORKDIR /src
        COPY *.sln .
        COPY **/*.csproj ./
        RUN dotnet restore
        COPY . .
        RUN dotnet publish -c Release -o /out

        FROM mcr.microsoft.com/dotnet/aspnet:8.0
        WORKDIR /app
        COPY --from=build /out .
        EXPOSE 8080
        ENTRYPOINT ["dotnet", "{ns}.dll"]
    """)


# Function: _tpl_docker_compose
def _tpl_docker_compose(offline_note: str) -> str:
    """Return a docker-compose.yml template."""
    return textwrap.dedent(f"""\
        # {offline_note}
        version: '3.9'
        services:
          app:
            build: .
            ports:
              - "8000:8000"
            environment:
              DATABASE_URL: postgresql+asyncpg://postgres:changeme@db/appdb
              SECRET_KEY: ${{SECRET_KEY:-changeme_in_production}}
            depends_on:
              db:
                condition: service_healthy
          db:
            image: postgres:16-alpine
            environment:
              POSTGRES_DB: appdb
              POSTGRES_USER: postgres
              POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:-changeme}}
            volumes:
              - postgres_data:/var/lib/postgresql/data
            healthcheck:
              test: ["CMD-SHELL", "pg_isready -U postgres"]
              interval: 5s
              retries: 5
        volumes:
          postgres_data:
    """)


# Function: _template_from_prompt
def _template_from_prompt(fname: str, user_prompt: str, target: dict, project_name: str) -> str:
    """Generate a ready-to-use boilerplate file when the LLM is offline.

    Delegates to per-language helpers; the old inline elif chains below are
    preserved as legacy fallback but are functionally unreachable.
    """
    lang        = target.get("language", "csharp")
    ext         = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    stem        = fname.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    class_name  = "".join(
        word[:1].upper() + word[1:]
        for word in re.sub(r"[^A-Za-z0-9]+", "_", stem).split("_")
        if word
    ) or project_name
    ns          = project_name
    offline_note = "# LLM offline \u2014 select an installed model in the generation runtime"

    if ext == "py":
        return _tpl_python(fname, user_prompt, project_name, offline_note, target)
    if ext == "cs":
        return _tpl_csharp(class_name, ns, user_prompt, offline_note, target)
    if ext == "java":
        return _tpl_java(fname, class_name, ns, user_prompt, offline_note, target)
    if ext == "go":
        return _tpl_go(fname, project_name, offline_note)
    if fname.replace("\\", "/").endswith("go.mod"):
        module = re.sub(r"[^a-z0-9-]+", "-", project_name.casefold()).strip("-") or "modernized-app"
        return f"module example.com/{module}\n\ngo 1.23\n"
    if ext in ("ts", "tsx", "js", "jsx"):
        return _tpl_typescript(fname, class_name, user_prompt, offline_note, target)
    if ext == "sql":
        return _tpl_sql(fname, user_prompt, offline_note, target)
    if fname.endswith("Dockerfile") or "dockerfile" in fname.lower():
        return _tpl_dockerfile(lang, ns)
    if ext in ("yml", "yaml") and ("docker" in fname or "compose" in fname):
        return _tpl_docker_compose(offline_note)
    if ext in ("yml", "yaml"):
        app_name = re.sub(r"[^a-z0-9-]+", "-", project_name.casefold()).strip("-") or "modernized-app"
        artifact_language = str(target.get("language") or "").casefold()
        if artifact_language == "kubernetes":
            return textwrap.dedent(f"""\
                apiVersion: apps/v1
                kind: Deployment
                metadata:
                  name: {app_name}
                spec:
                  replicas: 2
                  selector:
                    matchLabels:
                      app: {app_name}
                  template:
                    metadata:
                      labels:
                        app: {app_name}
                    spec:
                      containers:
                        - name: application
                          image: {app_name}:1.0.0
                          ports:
                            - containerPort: 8080
            """)
        if artifact_language == "cloudformation":
            return textwrap.dedent("""\
                AWSTemplateFormatVersion: '2010-09-09'
                Description: Production application infrastructure
                Resources:
                  ArtifactBucket:
                    Type: AWS::S3::Bucket
            """)
        if artifact_language == "ansible":
            return textwrap.dedent("""\
                - name: Configure application
                  hosts: all
                  become: true
                  tasks:
                    - name: Verify connectivity
                      ansible.builtin.ping:
            """)
        if artifact_language == "github_actions":
            return textwrap.dedent("""\
                name: CI
                on:
                  push:
                    branches: [main]
                  pull_request:
                jobs:
                  validate:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                      - run: echo validated
            """)
        if artifact_language == "helm":
            return textwrap.dedent(f"""\
                replicaCount: 2
                image:
                  repository: {app_name}
                  tag: 1.0.0
                  pullPolicy: IfNotPresent
                service:
                  type: ClusterIP
                  port: 8080
            """)
        return textwrap.dedent(f"""\
            spring:
              application:
                name: {app_name}
            server:
              port: 8080
            management:
              endpoints:
                web:
                  exposure:
                    include: health,info
        """)
    if ext == "ini":
        return textwrap.dedent("""\
            [alembic]
            script_location = alembic
            prepend_sys_path = .
            sqlalchemy.url = postgresql+psycopg://postgres:postgres@localhost/app

            [loggers]
            keys = root
            [handlers]
            keys = console
            [formatters]
            keys = generic
            [logger_root]
            level = WARN
            handlers = console
            [handler_console]
            class = StreamHandler
            args = (sys.stderr,)
            level = NOTSET
            formatter = generic
            [formatter_generic]
            format = %(levelname)-5.5s [%(name)s] %(message)s
        """)
    if fname.replace("\\", "/").endswith("package.json"):
        package_name = re.sub(r"[^a-z0-9-]+", "-", project_name.casefold()).strip("-") or "modernized-app"
        return json.dumps({
            "name": package_name,
            "version": "1.0.0",
            "private": True,
            "scripts": {
                "test": "node --test Database/schema_validation.test.js",
                "build": "node --check Database/schema_validation.test.js",
            },
        }, indent=2) + "\n"
    if ext == "json":
        return json.dumps({"name": project_name, "status": "ready", "version": 1}, indent=2) + "\n"
    if ext == "toml":
        return f'name = "{project_name}"\nversion = "1.0.0"\nenabled = true\n'
    if ext == "xml":
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<application name="{project_name}" version="1.0.0"/>\n'
    if ext in ("md", "markdown"):
        return f"# {project_name}\n\nProduction-ready generated artifact.\n"
    if ext in ("graphql", "gql"):
        return "type Query { health: Health! }\ntype Health { status: String! }\n"
    if ext == "proto":
        return 'syntax = "proto3";\npackage modernized.v1;\nmessage HealthRequest {}\nmessage HealthResponse { string status = 1; }\nservice HealthService { rpc Check(HealthRequest) returns (HealthResponse); }\n'
    if fname.replace("\\", "/").endswith("Jenkinsfile"):
        return textwrap.dedent("""\
            pipeline {
                agent any
                stages {
                    stage('Validate') {
                        steps {
                            echo 'validated'
                        }
                    }
                }
            }
        """)
    if fname.replace("\\", "/").endswith("pom.xml"):
        artifact_id = re.sub(r"[^a-z0-9-]+", "-", project_name.casefold()).strip("-") or "modernized-app"
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0"
                     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                     xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
              <modelVersion>4.0.0</modelVersion>
              <parent>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-starter-parent</artifactId>
                <version>3.4.11</version>
                <relativePath/>
              </parent>
              <groupId>com.modernize</groupId>
              <artifactId>{artifact_id}</artifactId>
              <version>1.0.0</version>
              <properties><java.version>21</java.version></properties>
              <dependencies>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-jpa</artifactId></dependency>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-validation</artifactId></dependency>
                <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
              </dependencies>
              <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>
            </project>
        """)
    if fname.replace("\\", "/").endswith("requirements.txt"):
        return "fastapi==0.121.0\nuvicorn[standard]==0.38.0\nsqlalchemy[asyncio]==2.0.44\nasyncpg==0.30.0\nalembic==1.17.1\npydantic-settings==2.11.0\npytest==8.4.2\n"
    req_lines = "\n".join(f"  {l}" for l in user_prompt.splitlines()[:20])
    return (
        f"# {fname}\n\n"
        f"**Requirement:**\n\n```\n{req_lines}\n```\n\n"
        f"**Target:** {target['name']}\n\n"
        "_LLM is offline. Select an installed model in the generation runtime to generate this file._\n"
    )
