import { memo, useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

type Props = {
  children?: string;
  className?: string;
  compact?: boolean;
  resolveImage?: (src?: string) => string;
};

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];
const COMPACT_COMPONENTS: Components = {
  p: ({ children: content }: { children?: ReactNode }) => <span>{content}</span>,
  strong: ({ children: content }: { children?: ReactNode }) => <strong>{content}</strong>,
  em: ({ children: content }: { children?: ReactNode }) => <em>{content}</em>
};

function replaceLatexCommand(value: string, command: string, marker: "*" | "**") {
  let output = value;
  const pattern = new RegExp(`\\\\(?:${command})\\{([^{}]*)\\}`, "g");
  for (let index = 0; index < 8; index += 1) {
    const next = output.replace(pattern, `${marker}$1${marker}`);
    if (next === output) break;
    output = next;
  }
  return output;
}

function transformOutsideMath(value: string, transform: (segment: string) => string) {
  const mathPattern = /(\$\$[\s\S]*?\$\$|\$[^$\n]*(?:\\.[^$\n]*)*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))/g;
  const fullMathPattern = /^(\$\$[\s\S]*?\$\$|\$[^$\n]*(?:\\.[^$\n]*)*\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))$/;
  return value
    .split(mathPattern)
    .map((segment) => (fullMathPattern.test(segment) ? segment : transform(segment)))
    .join("");
}

export function normalizeLatexMarkdown(value = "") {
  return transformOutsideMath(value, (segment) => {
    let output = segment;
    output = replaceLatexCommand(output, "textbf|mathbf", "**");
    output = replaceLatexCommand(output, "emph|textit", "*");
    output = output
      .replace(/\\%/g, "%")
      .replace(/\\&/g, "&")
      .replace(/\\_/g, "_")
      .replace(/\\#/g, "#")
      .replace(/\\texttt\{([^{}]*)\}/g, "`$1`")
      .replace(/([A-Za-z0-9])--([A-Za-z0-9])/g, "$1-$2");
    return output;
  });
}

export const MarkdownText = memo(function MarkdownText({
  children = "",
  className,
  compact = false,
  resolveImage
}: Props) {
  const normalized = useMemo(() => normalizeLatexMarkdown(children), [children]);
  const components: Components = useMemo(() => compact
    ? COMPACT_COMPONENTS
    : {
        img: ({ src, alt }: { src?: string; alt?: string }) => (
          <img src={resolveImage ? resolveImage(src) : src} alt={alt || ""} loading="lazy" />
        )
      }, [compact, resolveImage]);

  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={components}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  );
});
