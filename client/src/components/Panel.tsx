import type { CSSProperties, ReactNode } from "react";

type Props = {
  className?: string;
  children: ReactNode;
  style?: CSSProperties;
};

export function Panel({ className, children, style }: Props) {
  return (
    <div className={`panel ${className ?? ""}`} style={style}>
      {children}
    </div>
  );
}
