import { useEffect, useState } from "react";
import { Marp } from "@marp-team/marp-core";

// One renderer for every preview; raw HTML in the source is not rendered and no script is emitted.
const marp = new Marp({ html: false, script: false });

// Belt and braces on top of the empty sandbox: nothing is fetched, only inline styles and data: images.
const PREVIEW_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:";

function renderDoc(source: string): { doc: string; slides: number } {
  const { html, css } = marp.render(source);
  const doc =
    `<!doctype html><html><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}">` +
    `<style>${css}</style></head><body>${html}</body></html>`;
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
