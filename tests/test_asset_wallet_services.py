import asyncio
from pathlib import Path

from services.asset_service import AssetService
from services.billing_service import BillingService
from services.present_service import PresentService
from services.session_service import SessionService
from shinjuku_service import ShinjukuService
from services.user_service import UserService
from services.wallet_service import WalletService


def test_service_composes_asset_and_wallet_collaborators(tmp_path):
    service = ShinjukuService(str(tmp_path / "shinjuku.db"))

    assert isinstance(service.users, UserService)
    assert isinstance(service.assets, AssetService)
    assert isinstance(service.wallets, WalletService)
    assert isinstance(service.presents, PresentService)
    assert isinstance(service.sessions, SessionService)
    assert isinstance(service.billings, BillingService)


def test_collaborators_do_not_depend_on_facade_or_own_transactions():
    root = Path(__file__).resolve().parents[1]

    for name in (
        "user_service.py",
        "asset_service.py",
        "wallet_service.py",
        "present_service.py",
        "session_service.py",
        "billing_service.py",
    ):
        source = (root / "services" / name).read_text(encoding="utf-8")
        assert "shinjuku_service" not in source
        assert "_acquire" not in source
        assert ".transaction(" not in source


def test_implementation_modules_are_grouped_by_responsibility():
    root = Path(__file__).resolve().parents[1]

    assert {path.name for path in (root / "core").glob("*.py")} >= {
        "billing_engine.py",
        "constants.py",
        "errors.py",
        "money.py",
        "settings.py",
    }
    assert {path.name for path in (root / "infrastructure").glob("*.py")} >= {
        "migrations.py",
        "schema.py",
        "storage.py",
    }
    assert {path.name for path in (root / "adapters").glob("*.py")} >= {
        "event_adapter.py",
        "nickname_cache.py",
        "onebot_adapter.py",
    }
    assert not (root / "wallet_service.py").exists()
    assert not (root / "storage.py").exists()
    assert not (root / "event_adapter.py").exists()


def test_wallet_collaborator_uses_callers_transaction(tmp_path):
    async def scenario():
        service = ShinjukuService(str(tmp_path / "shinjuku.db"))
        try:
            await service.register("12345")
            async with service.storage.acquire() as conn:
                try:
                    async with conn.transaction(immediate=True):
                        await service.wallets.add_points("QQ:12345", 7, "rollback test", conn)
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass

            wallet = await service.wallet("QQ:12345")
            assert wallet["points"]["available"] == 0
        finally:
            await service.close()

    asyncio.run(scenario())
