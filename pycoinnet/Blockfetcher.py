
import asyncio
import logging
import weakref

from pycoinnet.msg.InvItem import InvItem, ITEM_TYPE_BLOCK


class Blockfetcher:
    """
    Blockfetcher

    This class parallelizes block fetching.
    When a new peer is connected, pass it in to add_peer
    and forward all messages of type "block" to handle_msg.

    To download a list of blocks, call "fetch_blocks".

    It accepts new peers via add_peer.

    It fetches new blocks via get_block_future or get_block.
    """
    def __init__(self, max_batch_size=200, initial_batch_size=1, target_batch_time=3, max_batch_timeout=12):
        # this queue accepts tuples of the form:
        #  (priority, InvItem(ITEM_TYPE_BLOCK, block_hash), future, peers_tried)
        self._block_hash_priority_queue = asyncio.PriorityQueue()
        self._retry_priority_queue = asyncio.PriorityQueue()
        self._get_batch_lock = asyncio.Lock()
        self._futures = weakref.WeakValueDictionary()
        self._max_batch_size = max_batch_size
        self._initial_batch_size = initial_batch_size
        self._target_batch_time = target_batch_time
        self._max_batch_timeout = max_batch_timeout

    def fetch_blocks(self, block_hash_priority_pair_list):
        """
        block_hash_priority_pair_list is a list of
        tuples with (block_hash, priority).
        The priority is generally expected block index.
        Blocks are prioritized by this priority.

        Returns: a list of futures, each corresponding to a tuple.
        """
        r = []
        for bh, pri in block_hash_priority_pair_list:
            f = asyncio.Future()
            peers_tried = set()
            item = (pri, bh, f, peers_tried)
            self._block_hash_priority_queue.put_nowait(item)
            f.item = item
            r.append(f)
            self._futures[bh] = f
        return r

    def add_peer(self, peer):
        """
        Register a new peer, and start the loop which polls it for blocks.
        """
        asyncio.get_event_loop().create_task(self._fetcher_loop(peer))

    def handle_msg(self, name, data):
        """
        When a peer gets a block message, it should invoked this method.
        """
        if name == 'block':
            block = data.get("block")
            bh = block.hash()
            f = self._futures.get(bh)
            if f and not f.done():
                f.set_result(block)
                del self._futures[bh]

    @asyncio.coroutine
    def _get_batch(self, batch_size, peer):
        with (yield from self._get_batch_lock):
            logging.info("getting batch up to size %d for %s", batch_size, peer)
            now = asyncio.get_event_loop().time()
            retry_time = now + self._max_batch_timeout

            # deal with retry queue
            while not self._retry_priority_queue.empty():
                retry_time, item = self._retry_priority_queue.get_nowait()
                if retry_time > now:
                    self._retry_priority_queue.put_nowait((retry_time, item))
                    break
                (pri, block_hash, block_future, peers_tried) = item
                if not block_future.done():
                    logging.info("timeout, retrying block %s", item[0])
                    self._block_hash_priority_queue.put_nowait(item)

            # build a batch
            skipped = []
            inv_items = []
            futures = []
            while len(futures) < batch_size:
                if self._block_hash_priority_queue.empty() and len(futures) > 0:
                    break
                item = yield from self._block_hash_priority_queue.get()
                (pri, block_hash, block_future, peers_tried) = item
                if block_future.done():
                    continue
                if peer in peers_tried:
                    logging.debug("block %s already tried by peer %s, skipping", item[0], peer)
                    skipped.append(item)
                    continue
                peers_tried.add(peer)
                inv_items.append(InvItem(ITEM_TYPE_BLOCK, block_hash))
                futures.append(block_future)
                self._retry_priority_queue.put_nowait((retry_time, item))
            for item in skipped:
                self._block_hash_priority_queue.put_nowait(item)
            logging.info("returning batch of size %d for %s", len(futures), peer)
        start_batch_time = asyncio.get_event_loop().time()
        peer.send_msg("getdata", items=inv_items)
        logging.debug("requested %s from %s", [f.item[0] for f in futures], peer)
        return futures, start_batch_time

    @asyncio.coroutine
    def _fetcher_loop(self, peer):
        batch_size = self._initial_batch_size
        loop = asyncio.get_event_loop()
        try:
            batch_1, start_batch_time_1 = yield from self._get_batch(batch_size=batch_size, peer=peer)
            while True:
                batch_2, start_batch_time_2 = yield from self._get_batch(batch_size=batch_size, peer=peer)
                yield from asyncio.wait(batch_1, timeout=self._max_batch_timeout)
                # see how many items we got
                item_count = sum(1 for f in batch_1 if f.done())
                # calculate new batch size
                batch_time = loop.time() - start_batch_time_1
                logging.info("got %d items from batch size %d in %s s",
                             item_count, len(batch_1), batch_time)
                time_per_item = batch_time / max(1, item_count)
                batch_size = min(int(self._target_batch_time / time_per_item) + 1, self._max_batch_size)
                batch_1 = batch_2
                logging.info("new batch size is %d", batch_size)
                start_batch_time_1 = start_batch_time_2
        except EOFError:
            logging.info("peer %s disconnected", peer)
        except Exception:
            logging.exception("problem with peer %s", peer)