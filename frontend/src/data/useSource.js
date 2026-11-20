import { useSyncExternalStore } from 'react';
import { getSource, subscribeSource } from './source.js';

export const useSource = () => useSyncExternalStore(subscribeSource, getSource, getSource);
