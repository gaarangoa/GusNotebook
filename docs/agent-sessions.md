# Agent sessions by folder

Use the small **Agent sessions** history button beside **Agent** and **Terminal**.
It lists saved conversations for the directory currently shown in **Files**.
Choose **Include subfolders** to search beneath it, filter by agent, or search
titles and session IDs. Click a conversation for a text preview, then **Resume
in terminal** to continue it. **All sessions** returns to the list; the history
button stays available after resuming.

Use the **pencil** button to rename a conversation and the **trash** button to
delete it. Both actions are also available in the preview. Names are saved in
the agent's own history, remain after restarting GusNotebook, and update any
open GusNotebook terminal tab for that conversation.

Deletion asks for confirmation and permanently removes the selected conversation
and its subagent history from the agent's local storage. It keeps notebooks and
project files. Close any GusNotebook terminal using that conversation before
deleting it; also stop it in an external agent application if it is open there.
Codex management requires a CLI version supporting `thread/name/set` and
`thread/delete`; update the CLI if it reports an unsupported action. Claude titles
use its native `custom-title` metadata records. These actions make no model requests.

Claude and Codex are supported, including conversations started in their own
CLIs outside GusNotebook. These are the histories of the user running
GusNotebook, on that computer. In a remote workspace, the list comes from the
remote computer. Other providers need a native-history adapter; they are not
automatically discovered yet.

GusNotebook reads Codex's `CODEX_HOME` (default `~/.codex`) and Claude's
`CLAUDE_CONFIG_DIR` (default `~/.claude`). Listing and previewing are read-only
and do not require a model request. The CLI must be installed to resume.
Archived Codex conversations and agent subprocess histories are omitted.
Previews show user and assistant text from a bounded recent excerpt, omitting
tool output and images; the agent itself restores the saved conversation.
Lists are paginated, and any discovery limit is shown in the dialog.

GusNotebook records the native session ID when its prompt hook runs. A project
association explicitly assigned to that terminal is then retained across
restarts and restored on resume. Existing or externally created conversations
without that association receive the ordinary agent setup; choosing a project
file does not add project rules. Resumed conversations still retain their prior
messages and instructions. Use a fresh agent for a clean conversation.

This unifies discovery and access. It does not combine separate conversations
or transfer one agent's internal state to another provider. GusNotebook's
**Sessions** sidebar continues to manage workspace tabs and terminals.
