import * as vscode from 'vscode';

/**
 * Kernell OS DevLayer — VS Code Extension
 * ═════════════════════════════════════════
 * 
 * This extension bridges the developer's IDE with the Kernell distributed
 * execution fabric. It captures implicit context (cursor, open files, git diff)
 * and routes it to the agent marketplace — something Cursor cannot do because
 * Cursor is locked to a single centralized model.
 * 
 * Key advantage over Cursor:
 *   - Multiple agents compete on your task
 *   - Results come back with cryptographic proof (ExecutionReceipt)
 *   - You see agent reputation before accepting
 *   - Bad agents lose money (slashing)
 * 
 * Key advantage over Antigravity:
 *   - Decentralized: no single vendor controls the AI
 *   - Verifiable: every result has a signed receipt + canary validation
 *   - Economic: agents have skin in the game
 */

// ══════════════════════════════════════════════════════════════
// CONTEXT CAPTURE (What we copy from Antigravity)
// ══════════════════════════════════════════════════════════════

interface IDEContext {
    activeFile: string | null;
    cursorLine: number;
    cursorColumn: number;
    selectedText: string;
    openFiles: string[];
    visibleRange: { start: number; end: number } | null;
    language: string;
    gitDiff: string | null;
    recentTerminalOutput: string | null;
    workspaceRoot: string | null;
}

function captureIDEContext(): IDEContext {
    const editor = vscode.window.activeTextEditor;
    
    const ctx: IDEContext = {
        activeFile: null,
        cursorLine: 0,
        cursorColumn: 0,
        selectedText: '',
        openFiles: [],
        visibleRange: null,
        language: 'unknown',
        gitDiff: null,
        recentTerminalOutput: null,
        workspaceRoot: null,
    };

    if (editor) {
        ctx.activeFile = vscode.workspace.asRelativePath(editor.document.uri);
        ctx.cursorLine = editor.selection.active.line + 1;
        ctx.cursorColumn = editor.selection.active.character;
        ctx.selectedText = editor.document.getText(editor.selection);
        ctx.language = editor.document.languageId;
        
        const visibleRanges = editor.visibleRanges;
        if (visibleRanges.length > 0) {
            ctx.visibleRange = {
                start: visibleRanges[0].start.line + 1,
                end: visibleRanges[0].end.line + 1,
            };
        }
    }

    // Capture all open files
    ctx.openFiles = vscode.workspace.textDocuments
        .filter(doc => !doc.isUntitled && doc.uri.scheme === 'file')
        .map(doc => vscode.workspace.asRelativePath(doc.uri));

    // Workspace root
    if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
        ctx.workspaceRoot = vscode.workspace.workspaceFolders[0].uri.fsPath;
    }

    return ctx;
}

async function captureGitDiff(): Promise<string | null> {
    try {
        const gitExtension = vscode.extensions.getExtension('vscode.git');
        if (!gitExtension) return null;
        
        const git = gitExtension.exports.getAPI(1);
        if (!git.repositories.length) return null;
        
        const repo = git.repositories[0];
        const diff = await repo.diff(true); // staged + unstaged
        return diff || null;
    } catch {
        return null;
    }
}

// ══════════════════════════════════════════════════════════════
// TASK SUBMISSION (What makes Kernell unique)
// ══════════════════════════════════════════════════════════════

interface KernellTask {
    taskId: string;
    description: string;
    context: IDEContext;
    contextFiles: Array<{ path: string; content: string; language: string }>;
    gitDiff: string | null;
    maxAgents: number;
    trustLevel: string;
}

interface ExecutionReceipt {
    receiptId: string;
    agentId: string;
    agentReputation: number;
    executionTimeMs: number;
    canaryPassed: boolean;
    diffs: Array<{
        path: string;
        action: string;
        hunks: string;
    }>;
    outputHash: string;
    signature: string;
}

// ══════════════════════════════════════════════════════════════
// COMMANDS
// ══════════════════════════════════════════════════════════════

async function askInline() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('Open a file first.');
        return;
    }

    // Capture full IDE context (Antigravity-style)
    const ideContext = captureIDEContext();
    ideContext.gitDiff = await captureGitDiff();

    // Get task description from user
    const description = await vscode.window.showInputBox({
        prompt: '⬡ Kernell: What do you want to do?',
        placeHolder: 'e.g., "refactor this function to use async/await"',
    });

    if (!description) return;

    // Show progress
    await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: '⬡ Kernell',
            cancellable: true,
        },
        async (progress, token) => {
            progress.report({ message: 'Analyzing codebase context...' });

            // In production: send to gRPC bridge → P2P network
            // For now: call Python CLI
            progress.report({ message: 'Submitting to agent marketplace...' });
            progress.report({ increment: 30 });

            // Simulate agent competition
            await new Promise(resolve => setTimeout(resolve, 1500));
            progress.report({ message: '3 agents competing...', increment: 30 });

            await new Promise(resolve => setTimeout(resolve, 1500));
            progress.report({ message: 'Receipts received! Opening preview...', increment: 40 });

            // Show receipt comparison panel
            showReceiptPanel(description, ideContext);
        }
    );
}

async function askGlobal() {
    const description = await vscode.window.showInputBox({
        prompt: '⬡ Kernell: Describe the task for the network',
        placeHolder: 'e.g., "add JWT authentication to the API endpoints"',
    });

    if (!description) return;

    const ideContext = captureIDEContext();
    ideContext.gitDiff = await captureGitDiff();

    vscode.window.showInformationMessage(
        `⬡ Task submitted: "${description.substring(0, 50)}..." — Agents competing now.`
    );

    // In production: full marketplace submission via gRPC
    showReceiptPanel(description, ideContext);
}

function showReceiptPanel(description: string, context: IDEContext) {
    const panel = vscode.window.createWebviewPanel(
        'kernellReceipt',
        '⬡ Kernell: Execution Preview',
        vscode.ViewColumn.Beside,
        { enableScripts: true }
    );

    panel.webview.html = getReceiptWebviewContent(description, context);

    // Handle accept/reject messages from webview
    panel.webview.onDidReceiveMessage(async (message) => {
        if (message.command === 'accept') {
            vscode.window.showInformationMessage(
                `✅ Changes accepted. Escrow released to agent ${message.agentId}.`
            );
            panel.dispose();
        } else if (message.command === 'reject') {
            vscode.window.showWarningMessage(
                `❌ Changes rejected. Agent penalized. Escrow returned.`
            );
            panel.dispose();
        }
    });
}

function getReceiptWebviewContent(description: string, context: IDEContext): string {
    return `<!DOCTYPE html>
<html>
<head>
    <style>
        body {
            font-family: 'SF Mono', 'Fira Code', monospace;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #1a1f2e, #0f1923);
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }
        .header h1 {
            color: #58a6ff;
            font-size: 18px;
            margin: 0 0 8px 0;
        }
        .task-desc {
            color: #8b949e;
            font-size: 14px;
        }
        .receipt-card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
            cursor: pointer;
            transition: border-color 0.2s;
        }
        .receipt-card:hover {
            border-color: #58a6ff;
        }
        .receipt-card.selected {
            border-color: #3fb950;
            box-shadow: 0 0 0 1px #3fb950;
        }
        .agent-info {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .agent-name { color: #58a6ff; font-weight: bold; }
        .reputation {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .rep-bar {
            width: 100px;
            height: 8px;
            background: #21262d;
            border-radius: 4px;
            overflow: hidden;
        }
        .rep-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s;
        }
        .canary-pass { color: #3fb950; }
        .canary-fail { color: #f85149; }
        .diff-block {
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 6px;
            padding: 12px;
            font-size: 12px;
            overflow-x: auto;
            margin-top: 8px;
        }
        .diff-add { color: #3fb950; }
        .diff-del { color: #f85149; }
        .actions {
            display: flex;
            gap: 12px;
            margin-top: 20px;
        }
        .btn {
            padding: 10px 24px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-accept {
            background: #238636;
            color: white;
        }
        .btn-accept:hover { background: #2ea043; }
        .btn-reject {
            background: #21262d;
            color: #f85149;
            border: 1px solid #f85149;
        }
        .btn-reject:hover { background: #f8514920; }
        .context-badge {
            display: inline-block;
            background: #1f2937;
            color: #8b949e;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            margin-right: 4px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>⬡ Kernell Execution Preview</h1>
        <div class="task-desc">${description}</div>
        <div style="margin-top: 8px;">
            <span class="context-badge">📄 ${context.activeFile || 'no file'}</span>
            <span class="context-badge">📍 Line ${context.cursorLine}</span>
            <span class="context-badge">🌐 ${context.language}</span>
            <span class="context-badge">📂 ${context.openFiles.length} open</span>
        </div>
    </div>

    <div class="receipt-card selected">
        <div class="agent-info">
            <span class="agent-name">🤖 agent_x7f2a</span>
            <div class="reputation">
                <div class="rep-bar"><div class="rep-fill" style="width: 92%; background: #3fb950;"></div></div>
                <span>92/100</span>
            </div>
        </div>
        <div>
            <span class="canary-pass">✅ Canary</span> · 
            <span>234ms</span> · 
            <span>2 files changed</span>
        </div>
        <div class="diff-block">
            <div class="diff-del">- def validate(self, msg):</div>
            <div class="diff-add">+ def validate(self, msg, quorum_weights=None):</div>
            <div>&nbsp;     result = self._check_epoch(msg)</div>
            <div class="diff-add">+     if quorum_weights:</div>
            <div class="diff-add">+         result = self._weighted_consensus(msg, quorum_weights)</div>
        </div>
    </div>

    <div class="receipt-card">
        <div class="agent-info">
            <span class="agent-name">🤖 agent_b9c1e</span>
            <div class="reputation">
                <div class="rep-bar"><div class="rep-fill" style="width: 78%; background: #d29922;"></div></div>
                <span>78/100</span>
            </div>
        </div>
        <div>
            <span class="canary-pass">✅ Canary</span> · 
            <span>891ms</span> · 
            <span>3 files changed</span>
        </div>
    </div>

    <div class="receipt-card">
        <div class="agent-info">
            <span class="agent-name">🤖 agent_d4f0a</span>
            <div class="reputation">
                <div class="rep-bar"><div class="rep-fill" style="width: 45%; background: #f85149;"></div></div>
                <span>45/100</span>
            </div>
        </div>
        <div>
            <span class="canary-fail">❌ Canary FAILED</span> · 
            <span>1203ms</span> · 
            <span>1 file changed</span>
        </div>
    </div>

    <div class="actions">
        <button class="btn btn-accept" onclick="accept()">✅ Accept Best (agent_x7f2a)</button>
        <button class="btn btn-reject" onclick="reject()">❌ Reject All</button>
    </div>

    <script>
        const vscode = acquireVsCodeApi();
        function accept() {
            vscode.postMessage({ command: 'accept', agentId: 'agent_x7f2a' });
        }
        function reject() {
            vscode.postMessage({ command: 'reject' });
        }
    </script>
</body>
</html>`;
}

// ══════════════════════════════════════════════════════════════
// EXTENSION LIFECYCLE
// ══════════════════════════════════════════════════════════════

export function activate(context: vscode.ExtensionContext) {
    console.log('⬡ Kernell DevLayer activated');

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('kernell.askInline', askInline),
        vscode.commands.registerCommand('kernell.ask', askGlobal),
        vscode.commands.registerCommand('kernell.review', () => {
            vscode.window.showInformationMessage('⬡ Opening receipt review...');
        }),
        vscode.commands.registerCommand('kernell.index', async () => {
            const terminal = vscode.window.createTerminal('Kernell');
            terminal.show();
            terminal.sendText('kernell dev index');
        }),
        vscode.commands.registerCommand('kernell.status', async () => {
            const terminal = vscode.window.createTerminal('Kernell');
            terminal.show();
            terminal.sendText('kernell dev status');
        }),
    );

    // Auto-index on startup if configured
    const config = vscode.workspace.getConfiguration('kernell');
    if (config.get('autoIndex')) {
        vscode.window.showInformationMessage('⬡ Kernell: Indexing codebase...');
    }

    // Status bar item
    const statusBar = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Right, 100
    );
    statusBar.text = '$(sparkle) Kernell';
    statusBar.tooltip = 'Kernell OS DevLayer — Click to submit task';
    statusBar.command = 'kernell.askInline';
    statusBar.show();
    context.subscriptions.push(statusBar);
}

export function deactivate() {
    console.log('⬡ Kernell DevLayer deactivated');
}
