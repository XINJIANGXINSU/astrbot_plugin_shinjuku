import asyncio
from pathlib import Path

from asset_service import AssetService
from billing_service import BillingService
from present_service import PresentService
from session_service import SessionService
from shinjuku_service import ShinjukuService
from user_service import UserService
from wallet_service import WalletService


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
        source = (root / name).read_text(encoding="utf-8")
        assert "shinjuku_service" not in source
        assert "_acquire" not in source
        assert ".transaction(" not in source


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
