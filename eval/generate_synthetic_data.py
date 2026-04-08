#!/usr/bin/env python3
"""
Generate realistic synthetic session data for watchdog.db + dashboard.jsonl.

Task scenarios are modeled after real engineering work (auth refactors, DB migrations,
security audits, etc.). Tool call sequences vary by effort level in ways that reflect
actual behavioral differences:
  - low:    jumps to edits quickly, minimal exploration
  - medium: some exploration, but cuts corners mid-session
  - high:   deep reads/greps before any edits, consistent throughout

Run: python3 generate_synthetic_data.py [--clear] [--n N]
  --clear  wipe existing synthetic entries before inserting
  --n N    sessions per effort level (default 60)
"""

import argparse
import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "watchdog.db"
DASHBOARD_PATH = Path(__file__).parent / "dashboard.jsonl"

# ── task scenarios ─────────────────────────────────────────────────────────────
# Each scenario defines realistic file paths and what "deep research" looks like.

SCENARIOS = [
    {
        "name": "auth-refactor",
        "description": "Refactor JWT auth middleware to support refresh tokens",
        "read_files": [
            "src/middleware/auth.ts", "src/middleware/auth.ts", "src/middleware/auth.ts",
            "src/routes/users.ts", "src/routes/users.ts",
            "src/models/user.model.ts", "src/models/token.model.ts",
            "tests/auth.test.ts", "tests/auth.test.ts",
            "src/config/jwt.ts", "src/utils/crypto.ts",
            "src/middleware/rateLimit.ts",
        ],
        "glob_patterns": [
            "src/middleware/*.ts", "src/**/*.ts", "tests/**/*.test.ts",
        ],
        "grep_patterns": [
            "verifyToken", "refreshToken", "jwt.sign", "Bearer",
        ],
        "write_files": [
            "src/middleware/auth.ts", "src/models/token.model.ts",
            "src/routes/auth.ts", "tests/auth.test.ts",
        ],
    },
    {
        "name": "db-migration",
        "description": "Add composite index + backfill nullable column on orders table",
        "read_files": [
            "migrations/20260301_orders.sql", "migrations/20260301_orders.sql",
            "src/models/order.model.ts", "src/models/order.model.ts",
            "src/services/order.service.ts", "src/services/order.service.ts",
            "src/repositories/order.repo.ts",
            "scripts/backfill_orders.ts",
            "docs/db-schema.md",
            "src/config/database.ts",
            "tests/order.integration.test.ts",
        ],
        "glob_patterns": [
            "migrations/*.sql", "src/models/*.ts", "src/repositories/*.ts",
        ],
        "grep_patterns": [
            "orders", "CREATE INDEX", "nullable", "backfill",
        ],
        "write_files": [
            "migrations/20260401_orders_index.sql",
            "src/models/order.model.ts",
            "scripts/backfill_orders.ts",
        ],
    },
    {
        "name": "api-rate-limiting",
        "description": "Implement per-user rate limiting with Redis sliding window",
        "read_files": [
            "src/middleware/rateLimit.ts", "src/middleware/rateLimit.ts",
            "src/config/redis.ts", "src/config/redis.ts",
            "src/middleware/auth.ts",
            "src/routes/api.ts", "src/routes/api.ts",
            "src/services/cache.service.ts",
            "tests/rateLimit.test.ts",
            "src/utils/logger.ts",
            "docs/api-limits.md",
            "src/types/express.d.ts",
        ],
        "glob_patterns": [
            "src/middleware/*.ts", "src/config/*.ts", "tests/*.test.ts",
        ],
        "grep_patterns": [
            "rateLim", "redis", "slidingWindow", "X-RateLimit",
        ],
        "write_files": [
            "src/middleware/rateLimit.ts",
            "src/services/rateLimitStore.ts",
            "tests/rateLimit.test.ts",
        ],
    },
    {
        "name": "security-audit",
        "description": "Audit and fix SQL injection vectors in search endpoints",
        "read_files": [
            "src/routes/search.ts", "src/routes/search.ts", "src/routes/search.ts",
            "src/repositories/search.repo.ts", "src/repositories/search.repo.ts",
            "src/repositories/user.repo.ts", "src/repositories/product.repo.ts",
            "src/utils/sanitize.ts",
            "src/middleware/validation.ts", "src/middleware/validation.ts",
            "tests/search.test.ts",
            "src/config/database.ts",
            "src/types/query.ts",
        ],
        "glob_patterns": [
            "src/repositories/*.ts", "src/routes/*.ts", "src/middleware/*.ts",
        ],
        "grep_patterns": [
            "query\\(", "raw(", "\\$\\{", "parameterized", "escape(",
        ],
        "write_files": [
            "src/repositories/search.repo.ts",
            "src/utils/sanitize.ts",
            "src/middleware/validation.ts",
            "tests/search.test.ts",
        ],
    },
    {
        "name": "data-pipeline-refactor",
        "description": "Refactor ETL pipeline to use streaming instead of batch loads",
        "read_files": [
            "pipelines/ingest.py", "pipelines/ingest.py", "pipelines/ingest.py",
            "pipelines/transform.py", "pipelines/transform.py",
            "pipelines/load.py",
            "pipelines/utils/chunker.py", "pipelines/utils/chunker.py",
            "pipelines/config.py",
            "tests/test_ingest.py", "tests/test_transform.py",
            "docs/pipeline-architecture.md",
            "pipelines/utils/validators.py",
        ],
        "glob_patterns": [
            "pipelines/**/*.py", "tests/test_*.py", "pipelines/utils/*.py",
        ],
        "grep_patterns": [
            "batch_size", "read_csv", "load_all", "stream", "yield",
        ],
        "write_files": [
            "pipelines/ingest.py", "pipelines/utils/chunker.py",
            "pipelines/transform.py", "tests/test_ingest.py",
        ],
    },
    {
        "name": "frontend-state-overhaul",
        "description": "Migrate Redux store to Zustand across dashboard components",
        "read_files": [
            "src/store/dashboardSlice.ts", "src/store/dashboardSlice.ts",
            "src/store/userSlice.ts",
            "src/components/Dashboard.tsx", "src/components/Dashboard.tsx",
            "src/components/MetricsPanel.tsx", "src/components/MetricsPanel.tsx",
            "src/components/FilterBar.tsx",
            "src/hooks/useDashboard.ts", "src/hooks/useDashboard.ts",
            "src/hooks/useFilters.ts",
            "src/types/dashboard.ts",
            "tests/Dashboard.test.tsx",
        ],
        "glob_patterns": [
            "src/store/*.ts", "src/components/**/*.tsx", "src/hooks/*.ts",
        ],
        "grep_patterns": [
            "useSelector", "useDispatch", "createSlice", "zustand", "create(",
        ],
        "write_files": [
            "src/store/dashboardStore.ts",
            "src/components/Dashboard.tsx",
            "src/components/MetricsPanel.tsx",
            "src/hooks/useDashboard.ts",
        ],
    },
    {
        "name": "ci-pipeline-debug",
        "description": "Fix flaky integration tests causing CI failures in prod deploys",
        "read_files": [
            ".github/workflows/ci.yml", ".github/workflows/ci.yml",
            "tests/integration/checkout.test.ts", "tests/integration/checkout.test.ts",
            "tests/integration/payment.test.ts", "tests/integration/payment.test.ts",
            "tests/helpers/testDb.ts", "tests/helpers/testDb.ts",
            "tests/helpers/mockServer.ts",
            "src/services/checkout.service.ts",
            "src/services/payment.service.ts",
            "jest.config.ts",
            "docker-compose.test.yml",
        ],
        "glob_patterns": [
            "tests/integration/*.test.ts", "tests/helpers/*.ts",
            ".github/workflows/*.yml",
        ],
        "grep_patterns": [
            "beforeEach", "afterAll", "setTimeout", "race condition", "flaky",
        ],
        "write_files": [
            "tests/helpers/testDb.ts",
            "tests/integration/checkout.test.ts",
            "jest.config.ts",
        ],
    },
    {
        "name": "caching-layer",
        "description": "Add multi-tier caching (memory + Redis) to product catalog API",
        "read_files": [
            "src/services/catalog.service.ts", "src/services/catalog.service.ts",
            "src/services/catalog.service.ts",
            "src/repositories/catalog.repo.ts", "src/repositories/catalog.repo.ts",
            "src/config/redis.ts",
            "src/middleware/cacheHeaders.ts",
            "src/routes/catalog.ts", "src/routes/catalog.ts",
            "tests/catalog.test.ts",
            "src/utils/serializer.ts",
            "src/types/catalog.ts",
            "docs/caching-strategy.md",
        ],
        "glob_patterns": [
            "src/services/*.ts", "src/repositories/*.ts", "src/config/*.ts",
        ],
        "grep_patterns": [
            "cache", "redis", "ttl", "invalidate", "stale-while-revalidate",
        ],
        "write_files": [
            "src/services/cache.service.ts",
            "src/services/catalog.service.ts",
            "src/middleware/cacheHeaders.ts",
            "tests/catalog.test.ts",
        ],
    },
    {
        "name": "observability-instrumentation",
        "description": "Add OpenTelemetry tracing to core service layer",
        "read_files": [
            "src/telemetry/tracer.ts", "src/telemetry/tracer.ts",
            "src/services/order.service.ts", "src/services/order.service.ts",
            "src/services/payment.service.ts",
            "src/services/inventory.service.ts",
            "src/middleware/requestLogger.ts", "src/middleware/requestLogger.ts",
            "src/config/telemetry.ts",
            "src/utils/logger.ts",
            "tests/telemetry.test.ts",
            "docker-compose.yml",
            "src/types/span.ts",
        ],
        "glob_patterns": [
            "src/services/*.ts", "src/telemetry/*.ts", "src/middleware/*.ts",
        ],
        "grep_patterns": [
            "span", "trace", "otel", "startSpan", "setAttribute",
        ],
        "write_files": [
            "src/telemetry/tracer.ts",
            "src/services/order.service.ts",
            "src/services/payment.service.ts",
            "src/middleware/requestLogger.ts",
        ],
    },
    {
        "name": "permission-rbac",
        "description": "Implement role-based access control with resource-level permissions",
        "read_files": [
            "src/auth/permissions.ts", "src/auth/permissions.ts", "src/auth/permissions.ts",
            "src/models/role.model.ts", "src/models/role.model.ts",
            "src/models/user.model.ts",
            "src/middleware/authorize.ts", "src/middleware/authorize.ts",
            "src/routes/admin.ts",
            "src/routes/users.ts",
            "tests/permissions.test.ts", "tests/permissions.test.ts",
            "docs/rbac-design.md",
            "src/types/auth.ts",
        ],
        "glob_patterns": [
            "src/auth/*.ts", "src/models/*.ts", "src/middleware/*.ts",
            "src/routes/*.ts",
        ],
        "grep_patterns": [
            "hasPermission", "role", "authorize", "policy", "resource",
        ],
        "write_files": [
            "src/auth/permissions.ts",
            "src/middleware/authorize.ts",
            "src/models/role.model.ts",
            "tests/permissions.test.ts",
        ],
    },
    # ── 30 additional scenarios ────────────────────────────────────────────────
    {
        "name": "graphql-schema-migration",
        "description": "Migrate REST endpoints to GraphQL with schema-first design",
        "read_files": [
            "src/schema/types.graphql", "src/schema/types.graphql",
            "src/resolvers/user.resolver.ts", "src/resolvers/user.resolver.ts",
            "src/resolvers/product.resolver.ts",
            "src/routes/users.ts", "src/routes/products.ts",
            "src/dataloaders/user.loader.ts",
            "src/context/auth.context.ts",
            "tests/graphql/user.test.ts",
            "src/schema/directives.ts",
            "src/utils/pagination.ts",
        ],
        "glob_patterns": [
            "src/schema/*.graphql", "src/resolvers/*.ts", "src/dataloaders/*.ts",
        ],
        "grep_patterns": [
            "resolver", "Query", "Mutation", "DataLoader", "context",
        ],
        "write_files": [
            "src/schema/types.graphql",
            "src/resolvers/user.resolver.ts",
            "src/dataloaders/user.loader.ts",
            "tests/graphql/user.test.ts",
        ],
    },
    {
        "name": "websocket-realtime",
        "description": "Add WebSocket support for real-time dashboard updates",
        "read_files": [
            "src/websocket/server.ts", "src/websocket/server.ts",
            "src/websocket/handlers/dashboard.ts", "src/websocket/handlers/dashboard.ts",
            "src/services/metrics.service.ts", "src/services/metrics.service.ts",
            "src/middleware/wsAuth.ts",
            "src/types/events.ts", "src/types/events.ts",
            "tests/websocket.test.ts",
            "src/config/server.ts",
            "src/utils/broadcast.ts",
        ],
        "glob_patterns": [
            "src/websocket/**/*.ts", "src/services/*.ts", "src/types/*.ts",
        ],
        "grep_patterns": [
            "ws.on", "emit", "broadcast", "socket", "subscribe",
        ],
        "write_files": [
            "src/websocket/server.ts",
            "src/websocket/handlers/dashboard.ts",
            "src/utils/broadcast.ts",
            "tests/websocket.test.ts",
        ],
    },
    {
        "name": "search-elasticsearch",
        "description": "Replace SQL full-text search with Elasticsearch indexing",
        "read_files": [
            "src/services/search.service.ts", "src/services/search.service.ts",
            "src/services/search.service.ts",
            "src/repositories/search.repo.ts", "src/repositories/search.repo.ts",
            "src/config/elasticsearch.ts",
            "src/models/product.model.ts",
            "src/jobs/reindex.job.ts", "src/jobs/reindex.job.ts",
            "tests/search.integration.test.ts",
            "src/utils/queryBuilder.ts",
            "docs/search-architecture.md",
        ],
        "glob_patterns": [
            "src/services/*.ts", "src/jobs/*.ts", "src/config/*.ts",
        ],
        "grep_patterns": [
            "index", "query", "elasticsearch", "bulk", "mapping",
        ],
        "write_files": [
            "src/services/search.service.ts",
            "src/jobs/reindex.job.ts",
            "src/config/elasticsearch.ts",
            "tests/search.integration.test.ts",
        ],
    },
    {
        "name": "multi-tenancy",
        "description": "Add multi-tenant support with row-level security in Postgres",
        "read_files": [
            "src/middleware/tenant.ts", "src/middleware/tenant.ts",
            "src/config/database.ts", "src/config/database.ts",
            "src/models/tenant.model.ts", "src/models/tenant.model.ts",
            "src/repositories/base.repo.ts", "src/repositories/base.repo.ts",
            "migrations/20260401_rls.sql",
            "src/types/context.ts",
            "tests/tenant.test.ts",
            "src/utils/schemaSwitch.ts",
        ],
        "glob_patterns": [
            "src/middleware/*.ts", "src/repositories/*.ts", "migrations/*.sql",
        ],
        "grep_patterns": [
            "tenant_id", "SET search_path", "row level", "POLICY", "current_setting",
        ],
        "write_files": [
            "migrations/20260401_rls.sql",
            "src/middleware/tenant.ts",
            "src/repositories/base.repo.ts",
            "tests/tenant.test.ts",
        ],
    },
    {
        "name": "event-sourcing",
        "description": "Implement event sourcing for order lifecycle with CQRS",
        "read_files": [
            "src/events/order.events.ts", "src/events/order.events.ts",
            "src/aggregates/order.aggregate.ts", "src/aggregates/order.aggregate.ts",
            "src/projections/order.projection.ts", "src/projections/order.projection.ts",
            "src/commands/order.commands.ts",
            "src/services/eventStore.service.ts", "src/services/eventStore.service.ts",
            "src/readModels/order.readModel.ts",
            "tests/order.aggregate.test.ts",
            "src/types/events.ts",
            "docs/event-sourcing-design.md",
        ],
        "glob_patterns": [
            "src/events/*.ts", "src/aggregates/*.ts", "src/projections/*.ts",
        ],
        "grep_patterns": [
            "apply(", "emit(", "snapshot", "replay", "EventStore",
        ],
        "write_files": [
            "src/aggregates/order.aggregate.ts",
            "src/projections/order.projection.ts",
            "src/services/eventStore.service.ts",
            "tests/order.aggregate.test.ts",
        ],
    },
    {
        "name": "container-health-checks",
        "description": "Add liveness/readiness probes and graceful shutdown to k8s deployment",
        "read_files": [
            "k8s/deployment.yaml", "k8s/deployment.yaml",
            "k8s/service.yaml",
            "src/health/health.controller.ts", "src/health/health.controller.ts",
            "src/health/checks/db.check.ts", "src/health/checks/db.check.ts",
            "src/health/checks/redis.check.ts",
            "src/server.ts", "src/server.ts",
            "src/config/gracefulShutdown.ts",
            "tests/health.test.ts",
            "Dockerfile",
        ],
        "glob_patterns": [
            "k8s/*.yaml", "src/health/**/*.ts", "src/config/*.ts",
        ],
        "grep_patterns": [
            "livenessProbe", "readinessProbe", "SIGTERM", "graceful", "healthz",
        ],
        "write_files": [
            "k8s/deployment.yaml",
            "src/health/health.controller.ts",
            "src/config/gracefulShutdown.ts",
            "tests/health.test.ts",
        ],
    },
    {
        "name": "async-job-queue",
        "description": "Replace synchronous email sending with Bull job queue",
        "read_files": [
            "src/jobs/email.job.ts", "src/jobs/email.job.ts",
            "src/services/email.service.ts", "src/services/email.service.ts",
            "src/services/email.service.ts",
            "src/config/bull.ts",
            "src/queues/emailQueue.ts", "src/queues/emailQueue.ts",
            "src/workers/email.worker.ts",
            "tests/email.job.test.ts",
            "src/utils/retry.ts",
            "src/types/jobs.ts",
        ],
        "glob_patterns": [
            "src/jobs/*.ts", "src/queues/*.ts", "src/workers/*.ts",
        ],
        "grep_patterns": [
            "queue.add", "process(", "Bull", "retry", "backoff",
        ],
        "write_files": [
            "src/queues/emailQueue.ts",
            "src/workers/email.worker.ts",
            "src/services/email.service.ts",
            "tests/email.job.test.ts",
        ],
    },
    {
        "name": "schema-validation",
        "description": "Add Zod schema validation to all API request/response bodies",
        "read_files": [
            "src/schemas/user.schema.ts", "src/schemas/user.schema.ts",
            "src/schemas/order.schema.ts", "src/schemas/order.schema.ts",
            "src/middleware/validate.ts", "src/middleware/validate.ts",
            "src/routes/users.ts", "src/routes/orders.ts",
            "src/types/api.ts", "src/types/api.ts",
            "tests/validation.test.ts",
            "src/utils/errors.ts",
            "src/config/openapi.ts",
        ],
        "glob_patterns": [
            "src/schemas/*.ts", "src/routes/*.ts", "src/middleware/*.ts",
        ],
        "grep_patterns": [
            "z.object", "zodSchema", "safeParse", "ZodError", "infer",
        ],
        "write_files": [
            "src/schemas/user.schema.ts",
            "src/middleware/validate.ts",
            "src/utils/errors.ts",
            "tests/validation.test.ts",
        ],
    },
    {
        "name": "feature-flags",
        "description": "Integrate LaunchDarkly feature flags into core checkout flow",
        "read_files": [
            "src/config/launchdarkly.ts", "src/config/launchdarkly.ts",
            "src/services/checkout.service.ts", "src/services/checkout.service.ts",
            "src/services/checkout.service.ts",
            "src/middleware/featureFlags.ts",
            "src/types/flags.ts", "src/types/flags.ts",
            "tests/checkout.test.ts", "tests/checkout.test.ts",
            "src/utils/flagDefaults.ts",
            "docs/feature-flags.md",
            "src/routes/checkout.ts",
        ],
        "glob_patterns": [
            "src/config/*.ts", "src/services/*.ts", "src/types/*.ts",
        ],
        "grep_patterns": [
            "variation(", "allFlags", "LDClient", "flagKey", "defaultValue",
        ],
        "write_files": [
            "src/config/launchdarkly.ts",
            "src/services/checkout.service.ts",
            "src/middleware/featureFlags.ts",
            "tests/checkout.test.ts",
        ],
    },
    {
        "name": "dependency-injection",
        "description": "Refactor service layer to use InversifyJS dependency injection",
        "read_files": [
            "src/inversify.config.ts", "src/inversify.config.ts",
            "src/services/user.service.ts", "src/services/user.service.ts",
            "src/services/order.service.ts",
            "src/repositories/user.repo.ts", "src/repositories/user.repo.ts",
            "src/types/interfaces.ts", "src/types/interfaces.ts",
            "src/decorators/injectable.ts",
            "tests/di.test.ts",
            "tsconfig.json",
            "src/container/symbols.ts",
        ],
        "glob_patterns": [
            "src/services/*.ts", "src/repositories/*.ts", "src/types/*.ts",
        ],
        "grep_patterns": [
            "@injectable", "@inject", "container.get", "Symbol(", "bind(",
        ],
        "write_files": [
            "src/inversify.config.ts",
            "src/types/interfaces.ts",
            "src/services/user.service.ts",
            "tests/di.test.ts",
        ],
    },
    {
        "name": "cdn-image-optimization",
        "description": "Add next/image optimization pipeline with CDN caching headers",
        "read_files": [
            "src/components/ProductImage.tsx", "src/components/ProductImage.tsx",
            "src/components/ProductImage.tsx",
            "src/utils/imageLoader.ts", "src/utils/imageLoader.ts",
            "next.config.js", "next.config.js",
            "src/hooks/useImageOptimize.ts",
            "src/types/image.ts",
            "tests/ProductImage.test.tsx",
            "src/config/cdn.ts",
            "public/images/",
        ],
        "glob_patterns": [
            "src/components/**/*.tsx", "src/utils/*.ts", "src/hooks/*.ts",
        ],
        "grep_patterns": [
            "next/image", "loader", "priority", "sizes", "blurDataURL",
        ],
        "write_files": [
            "src/utils/imageLoader.ts",
            "src/components/ProductImage.tsx",
            "next.config.js",
            "tests/ProductImage.test.tsx",
        ],
    },
    {
        "name": "pdf-generation",
        "description": "Add server-side PDF invoice generation with Puppeteer",
        "read_files": [
            "src/services/invoice.service.ts", "src/services/invoice.service.ts",
            "src/services/invoice.service.ts",
            "src/templates/invoice.html", "src/templates/invoice.html",
            "src/routes/invoices.ts",
            "src/models/invoice.model.ts", "src/models/invoice.model.ts",
            "src/config/puppeteer.ts",
            "tests/invoice.test.ts",
            "src/utils/currency.ts",
            "src/types/invoice.ts",
        ],
        "glob_patterns": [
            "src/services/*.ts", "src/templates/*.html", "src/models/*.ts",
        ],
        "grep_patterns": [
            "puppeteer", "page.pdf", "template", "handlebars", "generatePDF",
        ],
        "write_files": [
            "src/services/invoice.service.ts",
            "src/templates/invoice.html",
            "src/config/puppeteer.ts",
            "tests/invoice.test.ts",
        ],
    },
    {
        "name": "monorepo-shared-lib",
        "description": "Extract shared utilities into a monorepo package with Turborepo",
        "read_files": [
            "packages/shared/src/index.ts", "packages/shared/src/index.ts",
            "packages/shared/src/utils/date.ts", "packages/shared/src/utils/date.ts",
            "packages/shared/src/types/common.ts", "packages/shared/src/types/common.ts",
            "apps/web/src/utils/date.ts",
            "apps/api/src/utils/date.ts",
            "turbo.json", "turbo.json",
            "packages/shared/package.json",
            "tsconfig.base.json",
            "packages/shared/src/utils/validation.ts",
        ],
        "glob_patterns": [
            "packages/shared/src/**/*.ts", "apps/*/src/utils/*.ts", "*.json",
        ],
        "grep_patterns": [
            "from '@company/shared'", "workspace:*", "peerDependencies", "exports",
        ],
        "write_files": [
            "packages/shared/src/index.ts",
            "packages/shared/src/utils/date.ts",
            "apps/web/src/utils/date.ts",
            "turbo.json",
        ],
    },
    {
        "name": "e2e-test-setup",
        "description": "Set up Playwright E2E test suite for critical checkout paths",
        "read_files": [
            "e2e/checkout.spec.ts", "e2e/checkout.spec.ts",
            "e2e/checkout.spec.ts",
            "e2e/helpers/auth.helper.ts", "e2e/helpers/auth.helper.ts",
            "e2e/fixtures/products.json",
            "playwright.config.ts", "playwright.config.ts",
            "e2e/pages/checkout.page.ts", "e2e/pages/checkout.page.ts",
            "e2e/pages/cart.page.ts",
            ".github/workflows/e2e.yml",
            "e2e/helpers/db.helper.ts",
        ],
        "glob_patterns": [
            "e2e/**/*.ts", "e2e/pages/*.ts", "e2e/helpers/*.ts",
        ],
        "grep_patterns": [
            "page.goto", "expect(page", "test.beforeEach", "fixture", "locator",
        ],
        "write_files": [
            "e2e/checkout.spec.ts",
            "e2e/pages/checkout.page.ts",
            "playwright.config.ts",
            ".github/workflows/e2e.yml",
        ],
    },
    {
        "name": "payment-stripe-webhooks",
        "description": "Implement Stripe webhook handling for subscription lifecycle events",
        "read_files": [
            "src/webhooks/stripe.webhook.ts", "src/webhooks/stripe.webhook.ts",
            "src/webhooks/stripe.webhook.ts",
            "src/services/subscription.service.ts", "src/services/subscription.service.ts",
            "src/models/subscription.model.ts", "src/models/subscription.model.ts",
            "src/middleware/stripeVerify.ts", "src/middleware/stripeVerify.ts",
            "src/routes/webhooks.ts",
            "tests/stripe.webhook.test.ts",
            "src/config/stripe.ts",
            "src/types/stripe.ts",
        ],
        "glob_patterns": [
            "src/webhooks/*.ts", "src/services/*.ts", "src/middleware/*.ts",
        ],
        "grep_patterns": [
            "stripe.webhooks.constructEvent", "customer.subscription", "invoice.paid",
            "idempotency", "stripe-signature",
        ],
        "write_files": [
            "src/webhooks/stripe.webhook.ts",
            "src/services/subscription.service.ts",
            "src/middleware/stripeVerify.ts",
            "tests/stripe.webhook.test.ts",
        ],
    },
    {
        "name": "openapi-codegen",
        "description": "Generate TypeScript client SDK from OpenAPI spec with type safety",
        "read_files": [
            "openapi/spec.yaml", "openapi/spec.yaml", "openapi/spec.yaml",
            "openapi/paths/users.yaml", "openapi/paths/users.yaml",
            "openapi/components/schemas.yaml",
            "codegen/config.ts", "codegen/config.ts",
            "src/api/client.ts",
            "tests/generated/users.test.ts",
            "package.json",
            "src/types/generated.ts",
        ],
        "glob_patterns": [
            "openapi/**/*.yaml", "codegen/*.ts", "src/api/*.ts",
        ],
        "grep_patterns": [
            "operationId", "$ref", "components/schemas", "openapi-typescript", "orval",
        ],
        "write_files": [
            "openapi/spec.yaml",
            "codegen/config.ts",
            "src/types/generated.ts",
            "tests/generated/users.test.ts",
        ],
    },
    {
        "name": "audit-logging",
        "description": "Add tamper-evident audit log trail for GDPR compliance",
        "read_files": [
            "src/audit/audit.service.ts", "src/audit/audit.service.ts",
            "src/audit/audit.service.ts",
            "src/audit/audit.model.ts", "src/audit/audit.model.ts",
            "src/middleware/auditMiddleware.ts", "src/middleware/auditMiddleware.ts",
            "src/routes/admin.ts",
            "src/config/audit.ts",
            "migrations/20260402_audit_log.sql",
            "tests/audit.test.ts",
            "src/types/audit.ts",
            "docs/gdpr-compliance.md",
        ],
        "glob_patterns": [
            "src/audit/*.ts", "src/middleware/*.ts", "migrations/*.sql",
        ],
        "grep_patterns": [
            "auditLog", "HMAC", "actor", "resource", "immutable",
        ],
        "write_files": [
            "migrations/20260402_audit_log.sql",
            "src/audit/audit.service.ts",
            "src/middleware/auditMiddleware.ts",
            "tests/audit.test.ts",
        ],
    },
    {
        "name": "background-sync",
        "description": "Implement service worker background sync for offline form submission",
        "read_files": [
            "public/sw.js", "public/sw.js", "public/sw.js",
            "src/hooks/useOfflineSync.ts", "src/hooks/useOfflineSync.ts",
            "src/services/syncQueue.ts", "src/services/syncQueue.ts",
            "src/components/OfflineIndicator.tsx",
            "src/utils/idb.ts", "src/utils/idb.ts",
            "tests/sw.test.ts",
            "src/config/serviceWorker.ts",
            "src/types/sync.ts",
        ],
        "glob_patterns": [
            "public/sw.js", "src/hooks/*.ts", "src/utils/*.ts",
        ],
        "grep_patterns": [
            "sync.register", "BackgroundSync", "idb", "workbox", "queue",
        ],
        "write_files": [
            "public/sw.js",
            "src/services/syncQueue.ts",
            "src/hooks/useOfflineSync.ts",
            "tests/sw.test.ts",
        ],
    },
    {
        "name": "llm-streaming",
        "description": "Add streaming Claude API responses to chat feature with SSE",
        "read_files": [
            "src/routes/chat.ts", "src/routes/chat.ts",
            "src/services/llm.service.ts", "src/services/llm.service.ts",
            "src/services/llm.service.ts",
            "src/utils/sse.ts", "src/utils/sse.ts",
            "src/components/ChatMessage.tsx",
            "src/hooks/useChat.ts", "src/hooks/useChat.ts",
            "tests/chat.test.ts",
            "src/config/anthropic.ts",
            "src/types/chat.ts",
        ],
        "glob_patterns": [
            "src/routes/*.ts", "src/services/*.ts", "src/hooks/*.ts",
        ],
        "grep_patterns": [
            "stream: true", "on('text'", "SSE", "ReadableStream", "TextDecoder",
        ],
        "write_files": [
            "src/services/llm.service.ts",
            "src/utils/sse.ts",
            "src/hooks/useChat.ts",
            "tests/chat.test.ts",
        ],
    },
    {
        "name": "db-connection-pooling",
        "description": "Tune Postgres connection pool to fix connection exhaustion under load",
        "read_files": [
            "src/config/database.ts", "src/config/database.ts", "src/config/database.ts",
            "src/config/pool.ts", "src/config/pool.ts",
            "src/repositories/base.repo.ts", "src/repositories/base.repo.ts",
            "src/services/order.service.ts",
            "src/monitoring/pool.metrics.ts", "src/monitoring/pool.metrics.ts",
            "tests/pool.test.ts",
            "docs/db-performance.md",
            "src/utils/dbHealth.ts",
        ],
        "glob_patterns": [
            "src/config/*.ts", "src/repositories/*.ts", "src/monitoring/*.ts",
        ],
        "grep_patterns": [
            "max:", "idleTimeoutMillis", "connectionTimeoutMillis", "pool.totalCount",
            "pg.Pool",
        ],
        "write_files": [
            "src/config/pool.ts",
            "src/monitoring/pool.metrics.ts",
            "src/repositories/base.repo.ts",
            "tests/pool.test.ts",
        ],
    },
    {
        "name": "typed-env-config",
        "description": "Replace dotenv with typed environment config using t3-env/zod",
        "read_files": [
            "src/env.ts", "src/env.ts", "src/env.ts",
            "src/config/app.config.ts", "src/config/app.config.ts",
            ".env.example", ".env.example",
            "src/config/database.ts",
            "src/config/redis.ts",
            "src/config/jwt.ts",
            "tests/env.test.ts",
            "package.json",
            "src/types/env.d.ts",
        ],
        "glob_patterns": [
            "src/config/*.ts", "src/env.ts", ".env*",
        ],
        "grep_patterns": [
            "process.env", "z.string()", "createEnv", "ZodError", "parseEnv",
        ],
        "write_files": [
            "src/env.ts",
            "src/types/env.d.ts",
            "src/config/app.config.ts",
            "tests/env.test.ts",
        ],
    },
    {
        "name": "load-testing",
        "description": "Write k6 load test suite for API endpoints with SLO thresholds",
        "read_files": [
            "load-tests/checkout.k6.ts", "load-tests/checkout.k6.ts",
            "load-tests/checkout.k6.ts",
            "load-tests/scenarios/spike.ts", "load-tests/scenarios/spike.ts",
            "load-tests/helpers/auth.ts", "load-tests/helpers/auth.ts",
            "load-tests/config/thresholds.ts",
            "src/routes/checkout.ts",
            "src/routes/products.ts",
            "load-tests/scenarios/soak.ts",
            "docs/slo-definitions.md",
            "k6-config.json",
        ],
        "glob_patterns": [
            "load-tests/**/*.ts", "load-tests/scenarios/*.ts",
        ],
        "grep_patterns": [
            "http.post", "check(", "threshold", "vus", "iteration",
        ],
        "write_files": [
            "load-tests/checkout.k6.ts",
            "load-tests/scenarios/spike.ts",
            "load-tests/config/thresholds.ts",
            "k6-config.json",
        ],
    },
    {
        "name": "dark-mode-theming",
        "description": "Add dark mode with CSS variables and system preference detection",
        "read_files": [
            "src/styles/tokens.css", "src/styles/tokens.css", "src/styles/tokens.css",
            "src/hooks/useTheme.ts", "src/hooks/useTheme.ts",
            "src/components/ThemeProvider.tsx", "src/components/ThemeProvider.tsx",
            "src/components/ThemeToggle.tsx",
            "src/styles/global.css",
            "tailwind.config.js", "tailwind.config.js",
            "tests/ThemeProvider.test.tsx",
            "src/types/theme.ts",
        ],
        "glob_patterns": [
            "src/styles/*.css", "src/hooks/*.ts", "src/components/**/*.tsx",
        ],
        "grep_patterns": [
            "prefers-color-scheme", "--color-", "data-theme", "localStorage", "darkMode",
        ],
        "write_files": [
            "src/styles/tokens.css",
            "src/hooks/useTheme.ts",
            "src/components/ThemeProvider.tsx",
            "tests/ThemeProvider.test.tsx",
        ],
    },
    {
        "name": "micro-frontend",
        "description": "Extract product catalog into a Module Federation micro-frontend",
        "read_files": [
            "apps/catalog/webpack.config.js", "apps/catalog/webpack.config.js",
            "apps/catalog/webpack.config.js",
            "apps/shell/webpack.config.js", "apps/shell/webpack.config.js",
            "apps/catalog/src/bootstrap.tsx",
            "apps/catalog/src/components/ProductList.tsx", "apps/catalog/src/components/ProductList.tsx",
            "apps/shell/src/App.tsx",
            "apps/catalog/src/types/shared.ts",
            "tests/catalog/ProductList.test.tsx",
            "apps/catalog/src/store/catalog.store.ts",
        ],
        "glob_patterns": [
            "apps/*/webpack.config.js", "apps/*/src/**/*.tsx",
        ],
        "grep_patterns": [
            "ModuleFederationPlugin", "exposes", "remotes", "shared:", "bootstrap",
        ],
        "write_files": [
            "apps/catalog/webpack.config.js",
            "apps/catalog/src/components/ProductList.tsx",
            "apps/shell/webpack.config.js",
            "tests/catalog/ProductList.test.tsx",
        ],
    },
    {
        "name": "api-versioning",
        "description": "Introduce v2 API versioning with backwards-compatible routing",
        "read_files": [
            "src/routes/v1/users.ts", "src/routes/v1/users.ts",
            "src/routes/v2/users.ts", "src/routes/v2/users.ts",
            "src/middleware/apiVersion.ts", "src/middleware/apiVersion.ts",
            "src/transformers/user.v2.transformer.ts", "src/transformers/user.v2.transformer.ts",
            "src/config/router.ts",
            "tests/v2/users.test.ts", "tests/v2/users.test.ts",
            "docs/api-changelog.md",
            "src/types/v2.ts",
        ],
        "glob_patterns": [
            "src/routes/**/*.ts", "src/transformers/*.ts", "tests/**/*.test.ts",
        ],
        "grep_patterns": [
            "v2", "Accept-Version", "deprecated", "transformer", "ApiVersion",
        ],
        "write_files": [
            "src/routes/v2/users.ts",
            "src/transformers/user.v2.transformer.ts",
            "src/middleware/apiVersion.ts",
            "tests/v2/users.test.ts",
        ],
    },
    {
        "name": "cost-attribution",
        "description": "Add per-team AWS cost attribution tags to all infrastructure resources",
        "read_files": [
            "infra/main.tf", "infra/main.tf", "infra/main.tf",
            "infra/modules/rds/main.tf", "infra/modules/rds/main.tf",
            "infra/modules/ecs/main.tf", "infra/modules/ecs/main.tf",
            "infra/variables.tf", "infra/variables.tf",
            "infra/locals.tf",
            "infra/modules/elasticache/main.tf",
            "docs/tagging-strategy.md",
            "infra/modules/s3/main.tf",
        ],
        "glob_patterns": [
            "infra/**/*.tf", "infra/modules/**/*.tf",
        ],
        "grep_patterns": [
            "tags", "Team", "CostCenter", "Environment", "default_tags",
        ],
        "write_files": [
            "infra/locals.tf",
            "infra/variables.tf",
            "infra/modules/rds/main.tf",
            "infra/modules/ecs/main.tf",
        ],
    },
    {
        "name": "secret-rotation",
        "description": "Automate AWS Secrets Manager rotation for database credentials",
        "read_files": [
            "infra/secrets.tf", "infra/secrets.tf",
            "src/config/database.ts", "src/config/database.ts",
            "src/utils/secretsManager.ts", "src/utils/secretsManager.ts",
            "src/utils/secretsManager.ts",
            "lambda/rotate-secret/index.ts", "lambda/rotate-secret/index.ts",
            "infra/lambda.tf",
            "tests/secretsManager.test.ts",
            "src/config/retry.ts",
        ],
        "glob_patterns": [
            "infra/*.tf", "lambda/**/*.ts", "src/utils/*.ts",
        ],
        "grep_patterns": [
            "SecretsManager", "rotation", "AWSCURRENT", "AWSPENDING", "rotationLambdaARN",
        ],
        "write_files": [
            "lambda/rotate-secret/index.ts",
            "src/utils/secretsManager.ts",
            "infra/secrets.tf",
            "tests/secretsManager.test.ts",
        ],
    },
    {
        "name": "react-server-components",
        "description": "Migrate product listing page from client to React Server Components",
        "read_files": [
            "app/products/page.tsx", "app/products/page.tsx", "app/products/page.tsx",
            "app/products/ProductGrid.tsx", "app/products/ProductGrid.tsx",
            "app/products/ProductCard.tsx",
            "lib/products.ts", "lib/products.ts",
            "app/products/loading.tsx",
            "app/products/error.tsx",
            "tests/products/page.test.tsx",
            "app/layout.tsx",
            "lib/cache.ts",
        ],
        "glob_patterns": [
            "app/**/*.tsx", "lib/*.ts", "tests/**/*.test.tsx",
        ],
        "grep_patterns": [
            "'use client'", "async function", "Suspense", "cache(", "revalidate",
        ],
        "write_files": [
            "app/products/page.tsx",
            "app/products/ProductGrid.tsx",
            "lib/products.ts",
            "tests/products/page.test.tsx",
        ],
    },
    {
        "name": "contract-testing",
        "description": "Add Pact consumer-driven contract tests between API and mobile client",
        "read_files": [
            "pact/consumer/user.pact.ts", "pact/consumer/user.pact.ts",
            "pact/consumer/user.pact.ts",
            "pact/provider/user.provider.test.ts", "pact/provider/user.provider.test.ts",
            "pact/helpers/setup.ts", "pact/helpers/setup.ts",
            "src/routes/users.ts",
            "src/serializers/user.serializer.ts",
            "pact.config.ts",
            "tests/user.api.test.ts",
            ".github/workflows/pact.yml",
            "src/types/api.ts",
        ],
        "glob_patterns": [
            "pact/**/*.ts", "src/routes/*.ts", "src/serializers/*.ts",
        ],
        "grep_patterns": [
            "Pact(", "addInteraction", "publishPacts", "provider", "consumer",
        ],
        "write_files": [
            "pact/consumer/user.pact.ts",
            "pact/provider/user.provider.test.ts",
            "src/serializers/user.serializer.ts",
            ".github/workflows/pact.yml",
        ],
    },
]

# ── effort profiles ────────────────────────────────────────────────────────────
# Controls how many read/grep/glob calls happen before first write,
# and how ratio decays (or doesn't) across the session.

EFFORT_PROFILES = {
    "low": {
        "pre_write_reads":   (1, 3),    # reads before first write
        "reads_per_write":   (0.3, 1.2),
        "grep_probability":  0.15,
        "glob_probability":  0.10,
        "score_range":       (3, 5),
        "verdict_templates": [
            "Insufficient research before edits; jumped to implementation too quickly.",
            "Minimal exploration; wrote before understanding the full call graph.",
            "Low read:write ratio; likely missed edge cases in related files.",
            "Pattern: edit first, debug later. Several related files unread.",
            "Premature edits detected; auth-adjacent files not reviewed before changes.",
        ],
        "decay": "bad",    # ratio gets worse over session
    },
    "medium": {
        "pre_write_reads":   (4, 8),
        "reads_per_write":   (1.5, 3.5),
        "grep_probability":  0.35,
        "glob_probability":  0.25,
        "score_range":       (5, 7),
        "verdict_templates": [
            "Moderate research depth; some premature edits in second half of session.",
            "Good initial exploration but ratio dropped after first major edit.",
            "Reasonable depth for simple cases; complex interdependencies underexplored.",
            "Adequate reads before writes; test files reviewed but not exhaustively.",
            "Mid-session context loss; later edits made with less supporting reads.",
        ],
        "decay": "moderate",
    },
    "high": {
        "pre_write_reads":   (8, 16),
        "reads_per_write":   (4.0, 8.0),
        "grep_probability":  0.60,
        "glob_probability":  0.45,
        "score_range":       (7, 10),
        "verdict_templates": [
            "Strong research discipline; all related files reviewed before first edit.",
            "Thorough exploration including tests, config, and type definitions.",
            "High ratio maintained throughout; no premature writes detected.",
            "Deep grep usage to find all call sites before modifying signatures.",
            "Consistent depth early and late in session; no decay pattern observed.",
        ],
        "decay": "none",
    },
}

READ_TOOLS  = ["Read", "Glob", "Grep"]
WRITE_TOOLS = ["Write", "Edit", "MultiEdit"]


def build_tool_sequence(scenario: dict, profile: dict, rng: random.Random) -> list[tuple[str, str]]:
    """
    Returns list of (tool_name, file_path) tuples for one session.
    Sequence reflects realistic exploration → edit pattern.
    """
    reads_per_write = rng.uniform(*profile["reads_per_write"])
    n_writes = len(scenario["write_files"])
    n_reads_target = int(n_writes * reads_per_write)

    calls: list[tuple[str, str]] = []

    # --- Phase 1: exploration before first write ---
    pre_reads = rng.randint(*profile["pre_write_reads"])
    read_pool = scenario["read_files"][:]
    rng.shuffle(read_pool)

    for i in range(min(pre_reads, len(read_pool))):
        tool = "Read"
        if rng.random() < profile["grep_probability"]:
            tool = "Grep"
            path = rng.choice(scenario["grep_patterns"])
        elif rng.random() < profile["glob_probability"]:
            tool = "Glob"
            path = rng.choice(scenario["glob_patterns"])
        else:
            path = read_pool[i % len(read_pool)]
        calls.append((tool, path))

    # --- Phase 2: interleaved reads + writes ---
    write_pool = scenario["write_files"][:]
    remaining_reads = max(0, n_reads_target - pre_reads)
    reads_per_write_actual = max(1, remaining_reads // max(n_writes, 1))

    decay = profile["decay"]

    for wi, wfile in enumerate(write_pool):
        # reads before this write (decay means fewer reads as session goes on)
        if decay == "bad":
            count = max(0, reads_per_write_actual - wi * 2)
        elif decay == "moderate":
            count = max(1, reads_per_write_actual - wi)
        else:
            count = reads_per_write_actual + rng.randint(0, 2)

        for _ in range(count):
            if rng.random() < profile["grep_probability"]:
                tool = "Grep"
                path = rng.choice(scenario["grep_patterns"])
            elif rng.random() < profile["glob_probability"]:
                tool = "Glob"
                path = rng.choice(scenario["glob_patterns"])
            else:
                path = rng.choice(scenario["read_files"])
                tool = "Read"
            calls.append((tool, path))

        write_tool = rng.choice(["Edit", "Edit", "MultiEdit"]) if wi < len(write_pool) - 1 else rng.choice(["Write", "Edit"])
        calls.append((write_tool, wfile))

    return calls


def score_session(reads: int, writes: int, ratio: float, effort: str) -> int:
    profile = EFFORT_PROFILES[effort]
    lo, hi = profile["score_range"]
    # nudge score based on actual ratio vs expected
    base = (lo + hi) / 2
    expected_lo = profile["reads_per_write"][0]
    expected_hi = profile["reads_per_write"][1]
    expected_mid = (expected_lo + expected_hi) / 2
    delta = (ratio - expected_mid) / max(expected_mid, 1)
    score = base + delta * 1.5
    return max(lo, min(hi, round(score)))


def generate_sessions(
    n_per_effort: int = 15,
    seed: int = 42,
    start_date: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Returns (tool_call_rows, dashboard_entries)."""
    rng = random.Random(seed)
    if start_date is None:
        start_date = datetime(2026, 3, 9, tzinfo=timezone.utc)

    tool_rows: list[dict] = []
    dashboard: list[dict] = []

    efforts = ["low", "medium", "high"]
    scenarios = SCENARIOS[:]

    for effort in efforts:
        profile = EFFORT_PROFILES[effort]
        # spread sessions across ~30 days
        day_offsets = sorted(rng.sample(range(0, 30), min(n_per_effort, 30)))
        if len(day_offsets) < n_per_effort:
            day_offsets += [rng.randint(0, 30) for _ in range(n_per_effort - len(day_offsets))]
            day_offsets.sort()

        for i in range(n_per_effort):
            scenario = scenarios[i % len(scenarios)]
            session_id = f"syn-{effort[:3]}-{scenario['name'][:12]}-{i:03d}"
            ts_base = start_date + timedelta(days=day_offsets[i], hours=rng.randint(8, 18), minutes=rng.randint(0, 59))

            calls = build_tool_sequence(scenario, profile, rng)

            reads = sum(1 for t, _ in calls if t in ("Read", "Glob", "Grep"))
            writes = sum(1 for t, _ in calls if t in ("Write", "Edit", "MultiEdit"))
            ratio = round(reads / writes, 2) if writes else 0.0
            score = score_session(reads, writes, ratio, effort)
            verdict = rng.choice(profile["verdict_templates"])

            # tool_calls rows
            for j, (tool, path) in enumerate(calls):
                tool_rows.append({
                    "session_id": session_id,
                    "tool_name": tool,
                    "file_path": path,
                    "ts": ts_base.timestamp() + j * 12,
                })

            # dashboard entry
            dashboard.append({
                "ts": ts_base.isoformat(),
                "session_id": session_id,
                "scenario": scenario["name"],
                "effort_level": effort,
                "reads": reads,
                "writes": writes,
                "ratio": ratio,
                "score": score,
                "verdict": verdict,
            })

    return tool_rows, dashboard


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", action="store_true", help="Remove existing synthetic rows first")
    parser.add_argument("--n", type=int, default=60, help="Sessions per effort level (default 60)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    tool_rows, dashboard_entries = generate_sessions(n_per_effort=args.n, seed=args.seed)

    # ── write to watchdog.db ───────────────────────────────────────────────
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tool_name  TEXT NOT NULL,
            file_path  TEXT,
            ts         REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON tool_calls(session_id)")
    conn.commit()
    if args.clear:
        conn.execute("DELETE FROM tool_calls WHERE session_id LIKE 'syn-%'")
        print("Cleared existing synthetic tool_calls rows.")

    conn.executemany(
        "INSERT INTO tool_calls (session_id, tool_name, file_path, ts) VALUES (?,?,?,?)",
        [(r["session_id"], r["tool_name"], r["file_path"], r["ts"]) for r in tool_rows],
    )
    conn.commit()
    conn.close()
    print(f"Inserted {len(tool_rows)} tool_call rows into watchdog.db.")

    # ── write to dashboard.jsonl ───────────────────────────────────────────
    if args.clear:
        existing = []
        if DASHBOARD_PATH.exists():
            with open(DASHBOARD_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            e = json.loads(line)
                            if not e.get("session_id", "").startswith("syn-"):
                                existing.append(line)
                        except Exception:
                            existing.append(line)
        with open(DASHBOARD_PATH, "w") as f:
            for line in existing:
                f.write(line + "\n")
        print("Cleared existing synthetic dashboard.jsonl entries.")

    with open(DASHBOARD_PATH, "a") as f:
        for e in dashboard_entries:
            f.write(json.dumps(e) + "\n")
    print(f"Appended {len(dashboard_entries)} entries to dashboard.jsonl.")

    # ── quick summary ──────────────────────────────────────────────────────
    from collections import defaultdict
    by_effort: dict[str, list] = defaultdict(list)
    by_scenario: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for e in dashboard_entries:
        by_effort[e["effort_level"]].append(e)
        by_scenario[e["scenario"]][e["effort_level"]].append(e["ratio"])

    print()
    print(f"{'Effort':<10} {'N':>4} {'Avg ratio':>10} {'Avg score':>10} {'Min':>7} {'Max':>7}")
    print(f"{'-'*10} {'-'*4} {'-'*10} {'-'*10} {'-'*7} {'-'*7}")
    for effort in ["low", "medium", "high"]:
        group = by_effort[effort]
        ratios = [e["ratio"] for e in group]
        scores = [e["score"] for e in group]
        print(f"  {effort:<8} {len(group):>4} {sum(ratios)/len(ratios):>10.2f} "
              f"{sum(scores)/len(scores):>10.1f} {min(ratios):>7.2f} {max(ratios):>7.2f}")

    print()
    print(f"{'Scenario':<28} {'low avg':>9} {'med avg':>9} {'high avg':>9}  {'delta (hi-lo)':>14}")
    print(f"{'-'*28} {'-'*9} {'-'*9} {'-'*9}  {'-'*14}")
    for sc in sorted(by_scenario):
        d = by_scenario[sc]
        lo  = sum(d["low"])    / len(d["low"])    if d["low"]    else 0
        med = sum(d["medium"]) / len(d["medium"]) if d["medium"] else 0
        hi  = sum(d["high"])   / len(d["high"])   if d["high"]   else 0
        delta = hi - lo
        print(f"  {sc:<26} {lo:>9.2f} {med:>9.2f} {hi:>9.2f}  {delta:>+14.2f}")


if __name__ == "__main__":
    main()
