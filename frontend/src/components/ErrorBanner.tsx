interface Props {
  message: string | null;
  onClose: () => void;
}

export function ErrorBanner({ message, onClose }: Props) {
  if (!message) return null;
  return (
    <div className="banner banner-error" role="alert">
      <span>{message}</span>
      <button type="button" className="link" onClick={onClose} aria-label="Dismiss error">
        ×
      </button>
    </div>
  );
}
