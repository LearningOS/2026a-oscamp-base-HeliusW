//! # Async Channel
//!
//! In this exercise, you will use `tokio::sync::mpsc` async channels to implement producer-consumer pattern.
//!
//! ## Concepts
//! - `tokio::sync::mpsc::channel` creates bounded async channels
//! - Async `send` and `recv`
//! - Channel closing mechanism (receiver returns None after all senders are dropped)

use tokio::sync::mpsc;

/// Async producer-consumer:
/// - Create a producer task that sends each element from items sequentially
/// - Create a consumer task that receives all elements and collects them into Vec for return
///
/// Hint: Set channel capacity to items.len().max(1)
pub async fn producer_consumer(items: Vec<String>) -> Vec<String> {
    // TODO: Create channel with mpsc::channel
    let capacity=items.len().max(1);
    let (tx,mut rx)=mpsc::channel::<String>(capacity);
    // TODO: Spawn producer task: iterate through items, send each one
    let producer=tokio::spawn(async move{
        for item in items{
            tx.send(item).await.unwrap();
        }
    });

    // TODO: Spawn consumer task: loop recv until channel closes, collect results
    let consumer=tokio::spawn(async move{
        let mut results=Vec::new();
        while let Some(item)=rx.recv().await{
            results.push(item);
        }
        results
    });
    // TODO: Wait for consumer to complete and return results
    producer.await.unwrap();
    consumer.await.unwrap()

    //todo!()
}

/// Fan‑in pattern: multiple producers, one consumer.
/// Create `n_producers` producers, each sending `"producer {id}: message"`.
/// Consumer collects all messages, sorts them, and returns.
pub async fn fan_in(n_producers: usize) -> Vec<String> {
    // TODO: Create mpsc channel
    let (tx,mut rx)=mpsc::channel::<String>(n_producers.max(1));
    // TODO: Spawn n_producers producer tasks
    //       Each sends format!("producer {id}: message")
    let mut producers=Vec::with_capacity(n_producers);
    for id in 0..n_producers{
        let tx=tx.clone();
        let producer=tokio::spawn(async move{
            let message=format!("producer {}: message",id);
            tx.send(message).await.unwrap();
        });
        producers.push(producer)
    }
    // TODO: Drop the original sender (important! otherwise channel won't close)
    drop(tx);
    // TODO: Consumer loops receiving, collects and sorts
    let mut results=Vec::new();
    while let Some(message)=rx.recv().await{
        results.push(message);
    }
    for producer in producers{
        producer.await.unwrap();
    }
    results.sort();
    results
    //todo!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_producer_consumer() {
        let items = vec!["hello".into(), "async".into(), "world".into()];
        let result = producer_consumer(items.clone()).await;
        assert_eq!(result, items);
    }

    #[tokio::test]
    async fn test_producer_consumer_empty() {
        let result = producer_consumer(vec![]).await;
        assert!(result.is_empty());
    }

    #[tokio::test]
    async fn test_fan_in() {
        let result = fan_in(3).await;
        assert_eq!(
            result,
            vec![
                "producer 0: message",
                "producer 1: message",
                "producer 2: message",
            ]
        );
    }

    #[tokio::test]
    async fn test_fan_in_single() {
        let result = fan_in(1).await;
        assert_eq!(result, vec!["producer 0: message"]);
    }
}
