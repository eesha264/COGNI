import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import rehypeKatex from 'rehype-katex';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github-dark.css';
import 'katex/dist/katex.min.css';

// Split into its own lazily-loaded chunk (see MainChat.jsx's React.lazy import) —
// react-markdown + remark/rehype plugins + katex + highlight.js pushed the main
// bundle past 500KB, but none of it is needed until an AI message actually
// renders (e.g. not for the empty state or while a document is uploading).

// AI responses sometimes contain literal HTML tags (e.g. <br> inside a table cell,
// since markdown tables can't hold real line breaks). rehypeRaw parses that HTML;
// rehypeSanitize then strips anything unsafe (scripts, event handlers, iframes)
// before it's rendered, since this content comes from a model, not a trusted source.
// remark-math marks math content with specific classes (see mdast-util-math) that
// rehypeKatex still needs to find and render after sanitizing — allow-list exactly
// those values rather than any className, so this doesn't become a wider CSS-class
// injection surface for model-generated content.
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames || []), 'br'],
  attributes: {
    ...defaultSchema.attributes,
    div: [...(defaultSchema.attributes?.div || []), ['className', 'language-math', 'math-display']],
    span: [...(defaultSchema.attributes?.span || []), ['className', 'language-math', 'math-inline']],
  },
};

// remark-math only recognizes $...$ / $$...$$ delimiters, but models frequently
// write \(...\) / \[...\] instead regardless of prompt instructions — convert
// those to the delimiters remark-math understands before rendering.
// C16 fix: skip fenced code blocks (```...```) and inline code spans (`...`)
// so that source code containing \[ \] \( \) (regex, shell, LaTeX source) is
// not mangled into math delimiters.
const normalizeLatexDelimiters = (text) => {
  if (!text) return text;

  // Split on fenced code blocks (```...```), preserving the delimiters.
  const fenceSplit = text.split(/(```[\s\S]*?```)/g);
  return fenceSplit.map((segment, i) => {
    // Odd indices (by the capture group) are fenced code blocks — leave them.
    if (i % 2 === 1) return segment;

    // For the non-code segments, also protect inline code spans (`...`).
    const inlineSplit = segment.split(/(`[^`]*`)/g);
    return inlineSplit.map((seg, j) => {
      if (j % 2 === 1) return seg; // inline code — leave it
      return seg
        .replace(/\\\[/g, () => '$$')
        .replace(/\\\]/g, () => '$$')
        .replace(/\\\(/g, () => '$')
        .replace(/\\\)/g, () => '$');
    }).join('');
  }).join('');
};

// Renders AI Markdown responses: headers, bold/italic, tables, ordered/unordered
// lists, blockquotes, links, fenced code blocks with syntax highlighting, and
// LaTeX math ($...$ inline, $$...$$ block) typeset via KaTeX.
export default function MarkdownRenderer({ text }) {
  if (!text) return null;
  const normalized = normalizeLatexDelimiters(text);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema], rehypeKatex, rehypeHighlight]}
      components={{
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
        table: ({ node: _node, ...props }) => (
          <div className="table-scroll-wrapper">
            <table {...props} />
          </div>
        ),
      }}
    >
      {normalized}
    </ReactMarkdown>
  );
}
