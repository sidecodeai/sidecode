/** Where the router is — its path, query and fragment — written where a test can read it. */
import { useLocation } from 'react-router';

export function Address() {
  const location = useLocation();
  return <p data-testid="address">{`${location.pathname}${location.search}${location.hash}`}</p>;
}
