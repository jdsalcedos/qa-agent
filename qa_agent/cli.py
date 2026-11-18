"""Human-facing orchestration. Execution always crosses the real MCP client."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .design import DeepSeekDesigner, DesignError, design_plan
from .report import close_plan, close_run
from .security import redact, resolve_credentials
from .store import Store, load_profile, save_plan


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="qa-agent", description="Diseñar, revisar, aprobar y ejecutar QA local por MCP")
    commands = result.add_subparsers(dest="command", required=True)
    design = commands.add_parser("disenar", help="Proponer un plan con DeepSeek; nunca ejecutar el objetivo")
    design.add_argument("--requisitos", type=Path, required=True)
    design.add_argument("--contrato", type=Path, help="OpenAPI JSON local; opcional para diseño parcial")
    design.add_argument("--datos", type=Path, help="Datos sintéticos JSON/YAML, nunca credenciales")
    design.add_argument("--salida", type=Path, required=True)
    generate = commands.add_parser("generar", help="Exportar y registrar scripts, sin ejecución")
    generate.add_argument("--plan", type=Path, required=True)
    generate.add_argument("--salida", type=Path, required=True)
    approve = commands.add_parser("aprobar", help="Acto humano explícito ligado al hash de plan y manifiesto")
    approve.add_argument("--suite", required=True, help="ID registrado o directorio de suite")
    approve.add_argument("--responsable", required=True, help="Identidad del revisor humano")
    approve.add_argument("--confirmar", action="store_true", required=True,
                         help="Confirmo revisión humana del plan y todos los archivos generados")
    execute = commands.add_parser("ejecutar", help="Descubrir e invocar ejecutar_plan mediante MCP stdio real")
    execute.add_argument("--suite", required=True, help="ID registrado o directorio de suite")
    execute.add_argument("--entorno", required=True)
    close = commands.add_parser("cerrar", help="Reconstruir informe sin IA, red ni nuevas ejecuciones")
    source = close.add_mutually_exclusive_group(required=True)
    source.add_argument("--corrida")
    source.add_argument("--plan", type=Path)
    profile = commands.add_parser("perfil", help="Registrar JSON/YAML local o consultar perfil registrado")
    source = profile.add_mutually_exclusive_group(required=True)
    source.add_argument("--archivo", type=Path)
    source.add_argument("--entorno")
    return result


def _suite_id(value: str, store: Store) -> str:
    path = Path(value)
    if path.exists() or "/" in value or "\\" in value:
        return store.suite_id_for_path(path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = Store()
    secrets = tuple(filter(None, (os.environ.get("DEEPSEEK_API_KEY"),)))
    try:
        if args.command == "disenar":
            llm = DeepSeekDesigner()
            plan = design_plan(args.requisitos, args.contrato, args.datos, llm)
            output = save_plan(args.salida, plan)
            value = {"plan_path": str(output), "cases": len(plan.cases), "attempts": 0,
                     "pending_questions": plan.pending_questions, "approval": "Revisión humana pendiente"}
        elif args.command == "generar":
            from .generate import generate_suite
            manifest = generate_suite(args.plan, args.salida, store=store)
            value = {"suite_id": manifest.suite_id, "plan_hash": manifest.plan_hash,
                     "approval": "Revisión humana pendiente"}
        elif args.command == "aprobar":
            suite_id = _suite_id(args.suite, store)
            value = store.approve_suite(suite_id, approved_by=args.responsable)
        elif args.command == "ejecutar":
            suite_id = _suite_id(args.suite, store)
            store.verify_suite(suite_id)  # Preflight only: server must also enforce approval.
            profile = store.load_profile(args.entorno)
            secrets += tuple(filter(None, (os.environ.get(name) for name in profile.credentials.values())))
            from .mcp_client import execute_via_mcp
            value = execute_via_mcp(suite_id, args.entorno, store=store)
        elif args.command == "cerrar":
            output = close_run(args.corrida, store=store) if args.corrida else close_plan(args.plan)
            value = {"report_path": str(output)}
        elif args.command == "perfil":
            profile = load_profile(args.archivo) if args.archivo else store.load_profile(args.entorno)
            # Validate header/variable NAMES without requiring credentials now.
            resolve_credentials(profile, {name: "validation-only" for name in profile.credentials.values()})
            secrets += tuple(filter(None, (os.environ.get(name) for name in profile.credentials.values())))
            if args.archivo:
                public = profile.model_dump(mode="json", exclude={"credentials"})
                if redact(public, secrets) != public:
                    raise ValueError("Profile must not embed credential values")
                output = store.save_profile(profile)
                value = {"environment_id": profile.id, "profile_path": str(output)}
            else:
                value = profile
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", by_alias=True)
        print(json.dumps(redact(value, secrets), ensure_ascii=False, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("Interrumpido; conserve el historial y cierre la corrida guardada, sin reintentar mutaciones automáticamente.", file=sys.stderr)
        return 130
    except DesignError as exc:
        print(f"Error de diseño: {redact(str(exc), secrets)}", file=sys.stderr)
        return 1
    except Exception:
        # Never print provider/HTTP/subprocess exceptions containing unknown reflections.
        print("Error: entrada inválida, artefacto no íntegro, aprobación/perfil ausente o servicio no disponible. Revise la configuración y el historial local.", file=sys.stderr)
        return 1
