"""Canonical SQLite schema for a fresh Shinjuku database."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS "User" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "createdAt" TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS "Bind" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    bid TEXT NOT NULL,
    "userId" INTEGER NOT NULL REFERENCES "User"(id)
);
CREATE INDEX IF NOT EXISTS idx_bind_user ON "Bind"("userId");
CREATE INDEX IF NOT EXISTS idx_bind_type_bid ON "Bind"(type, bid);

CREATE TABLE IF NOT EXISTS "Session" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "userId" INTEGER NOT NULL REFERENCES "User"(id),
    "createdAt" TEXT NOT NULL,
    "closedAt" TEXT,
    "isActive" INTEGER,
    "billingCost" INTEGER,
    "finalCost" INTEGER,
    "CHECKCODE" TEXT,
    "doorOpened" INTEGER NOT NULL DEFAULT 0,
    "ENTRY_TYPE" TEXT NOT NULL DEFAULT 'normal'
);
CREATE INDEX IF NOT EXISTS idx_session_user_active ON "Session"("userId", "isActive");
CREATE INDEX IF NOT EXISTS idx_session_checkcode ON "Session"("CHECKCODE");

CREATE TABLE IF NOT EXISTS "Asset" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "assetId" INTEGER NOT NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    "billingEffect" TEXT,
    valid INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS "UserAsset" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "userId" INTEGER NOT NULL REFERENCES "User"(id),
    "assetDefId" INTEGER NOT NULL,
    "assetType" TEXT NOT NULL,
    "assetId" INTEGER REFERENCES "Asset"(id),
    count INTEGER NOT NULL DEFAULT 0,
    "activeAt" TEXT,
    "expireAt" TEXT
);
CREATE INDEX IF NOT EXISTS idx_userasset_user ON "UserAsset"("userId", "assetType");

CREATE TABLE IF NOT EXISTS "UserAssetLog" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "userId" INTEGER NOT NULL,
    "userAssetId" INTEGER,
    "assetId" INTEGER,
    "assetType" TEXT,
    "changeAmount" INTEGER NOT NULL DEFAULT 0,
    "countBefore" INTEGER NOT NULL DEFAULT 0,
    "countAfter" INTEGER NOT NULL DEFAULT 0,
    "expireAtBefore" TEXT,
    "expireAtAfter" TEXT,
    action TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS "Present" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    "oncePerUser" INTEGER NOT NULL DEFAULT 0,
    body TEXT
);

CREATE TABLE IF NOT EXISTS "Redeem" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    "presentId" INTEGER NOT NULL REFERENCES "Present"(id),
    "activeAt" TEXT,
    "expireAt" TEXT,
    "maxUseCount" INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_redeem_code ON "Redeem"(code);

CREATE TABLE IF NOT EXISTS "RedeemRecord" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "userId" INTEGER NOT NULL,
    "redeemId" INTEGER NOT NULL,
    "presentId" INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redeem_record_user ON "RedeemRecord"("userId", "presentId");

CREATE TABLE IF NOT EXISTS "SchemaMigration" (
    key TEXT PRIMARY KEY,
    "appliedAt" TEXT NOT NULL
);
"""
