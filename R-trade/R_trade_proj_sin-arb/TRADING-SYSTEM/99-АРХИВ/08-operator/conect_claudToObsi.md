Local REST API — для MCP связи
**ключ** В Obsidian → Settings → **Local REST API** → скопировать **API Key** (там уже сгенерирован)
C:\Users\[твоё_имя]\AppData\Roaming\Claude\claude_desktop_config.json
{
  "mcpServers": {
    "obsidian": {
      "command": "npx",
      "args": ["-y", "mcp-obsidian", "http://localhost:27123"],
      "env": {
        "OBSIDIAN_API_KEY": "вставь_ключ_из_Local_REST_API"
      }
    }
  }
}


закрыть и открыть Claude Desktop