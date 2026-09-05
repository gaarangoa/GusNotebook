try {
  const [state, view, language, commands, langPython, highlight, autocomplete] = await Promise.all([
    import("@codemirror/state"), import("@codemirror/view"),
    import("@codemirror/language"), import("@codemirror/commands"),
    import("@codemirror/lang-python"), import("@lezer/highlight"),
    import("@codemirror/autocomplete"),
  ]);
  const t = highlight.tags;
  // Our own palette rather than defaultHighlightStyle, whose colours assume a
  // different background than this app's light theme.
  const style = language.HighlightStyle.define([
    {tag: [t.keyword, t.moduleKeyword, t.controlKeyword], color: "#7c3aed"},
    {tag: [t.string, t.special(t.string)], color: "#0f7b3f"},
    {tag: [t.comment, t.lineComment, t.blockComment], color: "#8b95a5", fontStyle: "italic"},
    {tag: [t.number, t.bool, t.null], color: "#b45309"},
    {tag: [t.function(t.variableName), t.function(t.propertyName)], color: "#1d4ed8"},
    {tag: [t.className, t.typeName, t.definition(t.className)], color: "#0e7490"},
    {tag: t.operator, color: "#475569"},
    {tag: [t.self, t.atom], color: "#a21caf"},
    {tag: t.definition(t.variableName), color: "#0f172a"},
  ]);
  window.CM = {
    EditorState: state.EditorState, Prec: state.Prec,
    EditorView: view.EditorView, keymap: view.keymap,
    highlightActiveLine: view.highlightActiveLine,
    lineNumbers: view.lineNumbers,
    drawSelection: view.drawSelection, placeholder: view.placeholder,
    syntaxHighlighting: language.syntaxHighlighting,
    indentUnit: language.indentUnit, style,
    history: commands.history, undo: commands.undo, redo: commands.redo,
    undoDepth: commands.undoDepth, redoDepth: commands.redoDepth,
    indentMore: commands.indentMore, indentLess: commands.indentLess,
    defaultKeymap: commands.defaultKeymap, historyKeymap: commands.historyKeymap,
    python: langPython.python,
    syntaxTree: language.syntaxTree,
    autocompletion: autocomplete.autocompletion,
    startCompletion: autocomplete.startCompletion,
    autocompletionConfig: true,
  };
} catch (e) {
  window.CM = null;                      // the textarea fallback handles it
  console.warn('CodeMirror unavailable — falling back to plain textareas', e);
}
window.CM_READY = true;
window.dispatchEvent(new Event('cm-ready'));
