import { useEffect, useState } from "react";
import { Marp } from "@marp-team/marp-core";

// One renderer for every preview; raw HTML in the source is not rendered.
const marp = new Marp({ html: false });

function renderDoc(source: string): { doc: string; slides: number } {
  const { html, css } = marp.render(source);
  const doc = `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>${html}</body></html>`;
  return { doc, slides: (html.match(/<section/g) ?? []).length };
}

export function MarpPreview({ source }: { source: string }) {
  const [out, setOut] = useState<{ doc: string; slides: number } | null>(null);
  useEffect(() => {
    const t = setTimeout(() => setOut(renderDoc(source)), 300);
    return () => clearTimeout(t);
  }, [source]);
  return <iframe title="Preview" sandbox="" srcDoc={out?.doc ?? ""} data-slides={out?.slides ?? 0} />;
}
