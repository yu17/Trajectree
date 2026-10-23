import { useEffect, useRef } from "react";
import { EditorView, basicSetup } from "codemirror";
import { EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";

type Props = { value: string; onChange: (v: string) => void; label?: string };

// jsdom has no matchMedia (nor the layout CodeMirror measures); tests get a plain textarea.
const isJsdom = () => typeof window.matchMedia === "undefined";

function CodeMirrorEditor({ value, onChange, label }: Required<Props>) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const changed = useRef(onChange);
  changed.current = onChange;

  useEffect(() => {
    const v = new EditorView({
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          markdown(),
          EditorView.lineWrapping,
          // .cm-content already has role="textbox"; give it the accessible name
          EditorView.contentAttributes.of({ "aria-label": label }),
          EditorView.updateListener.of(u => {
            if (u.docChanged) changed.current(u.state.doc.toString());
          }),
        ],
      }),
      parent: host.current!,
    });
    view.current = v;
    return () => {
      v.destroy();
      view.current = null;
    };
    // Mount once; later value changes are dispatched below (the label is fixed per editor).
  }, []);

  useEffect(() => {
    const v = view.current;
    if (v && v.state.doc.toString() !== value) {
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } });
    }
  }, [value]);

  return <div ref={host} />;
}

export function MarkdownEditor({ value, onChange, label = "Source" }: Props) {
  if (isJsdom()) {
    return <textarea aria-label={label} rows={16} value={value} onChange={e => onChange(e.target.value)} />;
  }
  return <CodeMirrorEditor value={value} onChange={onChange} label={label} />;
}
