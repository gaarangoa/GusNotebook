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
    {tag: [t.keyword, t.moduleKeyword, t.controlKeyword], color: "var(--syntax-keyword)"},
    {tag: [t.string, t.special(t.string)], color: "var(--syntax-string)"},
    {tag: [t.comment, t.lineComment, t.blockComment], color: "var(--syntax-comment)", fontStyle: "italic"},
    {tag: [t.number, t.bool, t.null], color: "var(--syntax-number)"},
    {tag: [t.function(t.variableName), t.function(t.propertyName)], color: "var(--syntax-function)"},
    {tag: [t.className, t.typeName, t.definition(t.className)], color: "var(--syntax-type)"},
    {tag: t.operator, color: "var(--secondary)"},
    {tag: [t.self, t.atom], color: "var(--magenta)"},
    {tag: t.definition(t.variableName), color: "var(--text)"},
  ]);
  window.CM = {
    EditorState: state.EditorState, Prec: state.Prec, Compartment: state.Compartment,
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
