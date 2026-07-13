-- CASA PgGrantStore 参考 schema

CREATE TABLE IF NOT EXISTS grant_tools (
    agent_id TEXT NOT NULL,
    tool_id TEXT NOT NULL,
    surface TEXT DEFAULT 'harness',
    adapter TEXT DEFAULT 'native',
    enabled BOOLEAN DEFAULT TRUE,
    config_json JSONB DEFAULT '{}',
    PRIMARY KEY (agent_id, tool_id)
);

CREATE TABLE IF NOT EXISTS grant_data (
    agent_id TEXT PRIMARY KEY,
    read_artifacts JSONB DEFAULT '[]',
    write_artifact TEXT DEFAULT ''
);

-- 记录 agent 是否曾配置过 tool grants（含显式空列表）
CREATE TABLE IF NOT EXISTS grant_tool_agents (
    agent_id TEXT PRIMARY KEY
);
