// frontend/src/components/ImageViewer.jsx
import { useCallback, useEffect, useRef, useState } from "react";

const MIN_ZOOM = 0.2;
const MAX_ZOOM = 8;

/**
 * The original photo, zoomable and pannable.
 *
 * The whole point of the split view is checking a number against the paper, so
 * this has to reach a smudged digit quickly: wheel to zoom at the cursor, drag
 * to pan, double-click to toggle 1:1, and a rotate button for bills
 * photographed sideways.
 */
export function ImageViewer({ src, alt = "Original bill" }) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [rotation, setRotation] = useState(0);
  const dragging = useRef(null);
  const boxRef = useRef(null);

  const reset = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
    setRotation(0);
  }, []);

  // A new bill should not inherit the previous one's pan and zoom.
  useEffect(() => {
    reset();
  }, [src, reset]);

  const zoomAt = useCallback((factor, clientX, clientY) => {
    const box = boxRef.current;
    if (!box) return;
    const rect = box.getBoundingClientRect();
    // Where the cursor sits relative to the centre of the viewport.
    const cx = clientX - rect.left - rect.width / 2;
    const cy = clientY - rect.top - rect.height / 2;

    setZoom((z) => {
      const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * factor));
      const ratio = next / z;
      // Keep whatever is under the cursor under the cursor.
      setOffset((o) => ({
        x: cx - (cx - o.x) * ratio,
        y: cy - (cy - o.y) * ratio,
      }));
      return next;
    });
  }, []);

  // Registered natively because React's onWheel is passive and cannot
  // preventDefault, which would let the page scroll while zooming.
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onWheel = (e) => {
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX, e.clientY);
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const onPointerDown = (e) => {
    dragging.current = { x: e.clientX - offset.x, y: e.clientY - offset.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e) => {
    if (!dragging.current) return;
    setOffset({
      x: e.clientX - dragging.current.x,
      y: e.clientY - dragging.current.y,
    });
  };

  const onPointerUp = (e) => {
    dragging.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  if (!src) {
    return <div className="image-viewer image-viewer-empty">Loading image…</div>;
  }

  return (
    <div className="image-viewer">
      <div className="viewer-toolbar">
        <button type="button" onClick={() => zoomAt(1 / 1.4, 0, 0)} title="Zoom out">
          −
        </button>
        <span className="viewer-zoom">{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => zoomAt(1.4, 0, 0)} title="Zoom in">
          +
        </button>
        <button
          type="button"
          onClick={() => setRotation((r) => (r + 90) % 360)}
          title="Rotate 90°"
        >
          ↻
        </button>
        <button type="button" onClick={reset} title="Fit to window">
          Fit
        </button>
      </div>

      <div
        ref={boxRef}
        className="viewer-stage"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={(e) =>
          zoom === 1 ? zoomAt(2.5, e.clientX, e.clientY) : reset()
        }
        style={{ cursor: dragging.current ? "grabbing" : "grab" }}
      >
        <img
          src={src}
          alt={alt}
          draggable={false}
          style={{
            transform:
              `translate(${offset.x}px, ${offset.y}px) ` +
              `scale(${zoom}) rotate(${rotation}deg)`,
          }}
        />
      </div>
    </div>
  );
}
