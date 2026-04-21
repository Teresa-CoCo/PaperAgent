/* eslint-disable react/no-unknown-property */
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, MeshTransmissionMaterial, RoundedBox } from "@react-three/drei";
import { easing } from "maath";
import { memo, useRef } from "react";
import type { Group } from "three";

type FluidGlassLayerProps = {
  thickness?: number;
  scale?: number;
  ior?: number;
  anisotropy?: number;
};

const DEFAULTS = {
  thickness: 15,
  scale: 0.15,
  ior: 1,
  anisotropy: 0
};

export function FluidGlassLayer(props: FluidGlassLayerProps) {
  return (
    <div className="fluid-glass-layer" aria-hidden="true">
      <Canvas
        camera={{ position: [0, 0, 8], fov: 32 }}
        dpr={[1, 1.5]}
        gl={{ alpha: true, antialias: true, powerPreference: "high-performance" }}
      >
        <ambientLight intensity={0.8} />
        <directionalLight position={[2.5, 4, 5]} intensity={1.8} />
        <FluidSurface {...DEFAULTS} {...props} />
        <Environment preset="city" />
      </Canvas>
    </div>
  );
}

const FluidSurface = memo(function FluidSurface({
  thickness,
  scale,
  ior,
  anisotropy
}: Required<FluidGlassLayerProps>) {
  const groupRef = useRef<Group>(null);
  const lensRef = useRef<Group>(null);
  const { viewport } = useThree();

  useFrame(({ pointer }, delta) => {
    if (groupRef.current) {
      easing.damp(groupRef.current.rotation, "x", pointer.y * 0.025, 0.18, delta);
      easing.damp(groupRef.current.rotation, "y", -pointer.x * 0.025, 0.18, delta);
    }
    if (lensRef.current) {
      easing.damp3(
        lensRef.current.position,
        [pointer.x * viewport.width * 0.22, pointer.y * viewport.height * 0.18, 0.14],
        0.12,
        delta
      );
    }
  });

  const materialProps = {
    transmission: 1,
    roughness: 0.02,
    thickness,
    ior,
    anisotropy,
    chromaticAberration: 0.06,
    distortion: 0.16,
    distortionScale: 0.18,
    temporalDistortion: 0.08,
    color: "#ffffff",
    attenuationColor: "#dffbf0",
    attenuationDistance: 0.42
  };

  return (
    <group ref={groupRef}>
      <RoundedBox
        args={[viewport.width * 1.08, viewport.height * 1.08, 0.08]}
        radius={0.08}
        smoothness={10}
        position={[0, 0, 0]}
      >
        <MeshTransmissionMaterial {...materialProps} />
      </RoundedBox>
      <group ref={lensRef}>
        <mesh scale={Math.max(viewport.width, viewport.height) * scale}>
          <sphereGeometry args={[1, 48, 24]} />
          <MeshTransmissionMaterial
            {...materialProps}
            roughness={0}
            thickness={thickness * 0.55}
            chromaticAberration={0.08}
            distortion={0.24}
            distortionScale={0.22}
          />
        </mesh>
      </group>
    </group>
  );
});
