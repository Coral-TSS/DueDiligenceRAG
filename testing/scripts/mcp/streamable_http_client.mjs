/**
 * 用 @modelcontextprotocol/client 的 Client 和 StreamableHTTPClientTransport 连 /mcp。
 *
 * 需要环境变量 MCP_URL（例如 http://127.0.0.1:8766/mcp）和 DDA_MCP_API_KEY。
 * 默认客户端走 2025 的 initialize。再跑一遍 versionNegotiation: auto，
 * 与 @deepseek-ai/dsh-mcp-client 0.2.2-rc.1 的连接方式相同。
 * Client.close() 在 SDK 2.2.0 里不发 DELETE；这里在关闭传输前调用 terminateSession()。
 */
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';

const url = process.env.MCP_URL;
const key = process.env.DDA_MCP_API_KEY;
const EXPECTED = ['coverage_gaps', 'list_documents', 'list_projects', 'list_subjects', 'read_evidence', 'search_evidence'];

if (!url || !key) {
  console.error('需要环境变量 MCP_URL 和 DDA_MCP_API_KEY');
  process.exit(2);
}

function transport() {
  return new StreamableHTTPClientTransport(new URL(url), {
    requestInit: { headers: { Authorization: `Bearer ${key}` } },
  });
}

async function connectAndCheck(mode) {
  const link = transport();
  const originalClose = link.close.bind(link);
  let closed = false;
  let followUpStatus = null;
  link.close = async () => {
    if (closed) return;
    closed = true;
    const sessionId = link.sessionId;
    if (sessionId) {
      // SDK 2.2.0 的 Client.close() 不发 DELETE。terminateSession() 才发。
      // 成功时响应是 2xx（本服务为 204）；405 表示服务端拒绝结束会话。
      await link.terminateSession();
      const follow = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${key}`,
          Accept: 'application/json, text/event-stream',
          'Content-Type': 'application/json',
          'Mcp-Session-Id': sessionId,
          'MCP-Protocol-Version': '2025-06-18',
        },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'ping' }),
      });
      followUpStatus = follow.status;
      if (follow.status !== 404) {
        throw new Error(`DELETE 之后会话仍可用，HTTP ${follow.status}`);
      }
    }
    await originalClose();
  };
  const options = mode === 'auto' ? { versionNegotiation: { mode: 'auto' } } : {};
  const client = new Client({ name: 'dd-agent-streamable-check', version: '0.1.0' }, options);
  try {
    await client.connect(link);
    if (!link.sessionId) throw new Error('initialize 响应没有 Mcp-Session-Id');
    if (link.protocolVersion !== '2025-06-18') {
      throw new Error(`协商版本不是 2025-06-18：${link.protocolVersion}`);
    }
    const listed = await client.listTools();
    const names = listed.tools.map((tool) => tool.name);
    const actual = [...names].sort();
    const expected = [...EXPECTED].sort();
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new Error(`工具列表不符：${names.join(',')}`);
    }
    const result = await client.callTool({ name: 'list_projects', arguments: {} });
    if (result.isError) throw new Error(`list_projects 返回 isError：${JSON.stringify(result.content)}`);
    const text = result.content?.[0]?.text;
    const payload = JSON.parse(text);
    if (!Array.isArray(payload.projects)) throw new Error('list_projects 的正文不是含 projects 数组的 JSON');
    await client.close();
    if (followUpStatus !== 404) throw new Error('关闭时没有结束会话');
    return {
      mode,
      protocolVersion: link.protocolVersion,
      tools: names,
      projectCount: payload.projects.length,
      deleteSent: true,
      followUpStatus,
    };
  } finally {
    await link.close().catch(() => {});
  }
}

const legacy = await connectAndCheck('legacy');
const auto = await connectAndCheck('auto');
console.log(JSON.stringify({ legacy, auto }));
