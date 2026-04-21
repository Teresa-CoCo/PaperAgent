import { LiquidGlassContainer } from "@tinymomentum/liquid-glass-react";
import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { FluidGlassLayer } from "./FluidGlassLayer";

type Props = {
  className: string;
  children: ReactNode;
  style?: CSSProperties;
};

export function GlassPanel({ className, children, style }: Props) {
  const frameRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 320, height: 240 });

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame) return undefined;

    const syncSize = (width: number, height: number) => {
      setSize({
        width: Math.max(1, Math.round(width)),
        height: Math.max(1, Math.round(height))
      });
    };

    const rect = frame.getBoundingClientRect();
    syncSize(rect.width, rect.height);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      syncSize(entry.contentRect.width, entry.contentRect.height);
    });

    observer.observe(frame);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={frameRef} className={className} style={style}>
      <LiquidGlassContainer
        width={size.width}
        height={size.height}
        borderRadius={8}
        innerShadowColor="rgba(255,255,255,0.82)"
        innerShadowBlur={24}
        innerShadowSpread={-5}
        glassTintColor="#ffffff"
        glassTintOpacity={34}
        frostBlurRadius={26}
        noiseFrequency={0.008}
        noiseStrength={96}
        className="liquid-panel-surface"
        style={{ borderRadius: 8 }}
      >
        <FluidGlassLayer thickness={15} scale={0.15} ior={1} anisotropy={0} />
        <div className="liquid-panel-content">
          {children}
        </div>
      </LiquidGlassContainer>
    </div>
  );
}
